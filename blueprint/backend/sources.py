"""
Search for software built from source to include in the blueprint as a tarball.
"""

import errno
import glob
import hashlib
import logging
import os
import os.path
import re
import shutil
import stat
import subprocess
import tarfile

from blueprint import context_managers
from blueprint import ignore
from blueprint import util


def _source(b, dirname, old_cwd):
    tmpname = os.path.join(os.getcwd(), dirname[1:].replace('/', '-'))

    exclude = []

    pattern_pip = re.compile(r'\.egg-info/installed-files.txt$')
    pattern_egg = re.compile(r'\.egg(?:-info)?(?:/|$)')
    pattern_pth = re.compile(
        r'lib/python[^/]+/(?:dist|site)-packages/easy-install.pth$')
    pattern_bin = re.compile(
        r'EASY-INSTALL(?:-ENTRY)?-SCRIPT|This file was generated by RubyGems')

    # Create a partial shallow copy of the directory.
    for dirpath, dirnames, filenames in os.walk(dirname):

        # Definitely ignore the shallow copy directory.
        if dirpath.startswith(tmpname):
            continue

        # Determine if this entire directory should be ignored by default.
        ignored = ignore.file(dirpath)

        dirpath2 = os.path.normpath(
            os.path.join(tmpname, os.path.relpath(dirpath, dirname)))

        # Create this directory in the shallow copy with matching mode, owner,
        # and owning group.  Suggest running as `root` if this doesn't work.
        os.mkdir(dirpath2)
        s = os.lstat(dirpath)
        try:
            try:
                os.lchown(dirpath2, s.st_uid, s.st_gid)
            except OverflowError:
                logging.warning('{0} has uid:gid {1}:{2} - using chown(1)'.
                                format(dirpath, s.st_uid, s.st_gid))
                p = subprocess.Popen(['chown',
                                      '{0}:{1}'.format(s.st_uid, s.st_gid),
                                      dirpath2],
                                     close_fds=True)
                p.communicate()
            os.chmod(dirpath2, s.st_mode)
        except OSError as e:
            logging.warning('{0} caused {1} - try running as root'.
                            format(dirpath, errno.errorcode[e.errno]))
            return

        for filename in filenames:
            pathname = os.path.join(dirpath, filename)

            if ignore.source(pathname, ignored):
                continue

            pathname2 = os.path.join(dirpath2, filename)

            # Exclude files that are part of the RubyGems package.
            for globname in (
                os.path.join('/usr/lib/ruby/gems/*/gems/rubygems-update-*/lib',
                             pathname[1:]),
                os.path.join('/var/lib/gems/*/gems/rubygems-update-*/lib',
                             pathname[1:])):
                if 0 < len(glob.glob(globname)):
                    continue

            # Remember the path to all of `pip`'s `installed_files.txt` files.
            if pattern_pip.search(pathname):
                exclude.extend([os.path.join(dirpath2, line.rstrip())
                    for line in open(pathname)])

            # Likewise remember the path to Python eggs.
            if pattern_egg.search(pathname):
                exclude.append(pathname2)

            # Exclude `easy_install`'s bookkeeping file, too.
            if pattern_pth.search(pathname):
                continue

            # Exclude executable placed by Python packages or RubyGems.
            if pathname.startswith('/usr/local/bin/'):
                try:
                    if pattern_bin.search(open(pathname).read()):
                        continue
                except IOError as e:
                    pass

            # Exclude share/applications/mimeinfo.cache, whatever that is.
            if '/usr/local/share/applications/mimeinfo.cache' == pathname:
                continue

            # Clean up dangling symbolic links.  This makes the assumption
            # that no one intends to leave dangling symbolic links hanging
            # around, which I think is a good assumption.
            s = os.lstat(pathname)
            if stat.S_ISLNK(s.st_mode):
                try:
                    os.stat(pathname)
                except OSError as e:
                    if errno.ENOENT == e.errno:
                        logging.warning('ignored dangling symbolic link {0}'.
                                        format(pathname))
                        continue

            # Hard link this file into the shallow copy.  Suggest running as
            # `root` if this doesn't work though in practice the check above
            # will have already caught this problem.
            try:
                os.link(pathname, pathname2)
            except OSError as e:
                logging.warning('{0} caused {1} - try running as root'.
                                format(pathname, errno.errorcode[e.errno]))
                return

    # Unlink files that were remembered for exclusion above.
    for pathname in exclude:
        try:
            os.unlink(pathname)
        except OSError as e:
            if e.errno not in (errno.EISDIR, errno.ENOENT):
                raise e

    # Remove empty directories.  For any that hang around, match their
    # access and modification times to the source, otherwise the hash of
    # the tarball will not be deterministic.
    for dirpath, dirnames, filenames in os.walk(tmpname, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            s = os.lstat(os.path.join(dirname, os.path.relpath(dirpath,
                                                               tmpname)))
            os.utime(dirpath, (s.st_atime, s.st_mtime))

    # If the shallow copy of still exists, create a tarball named by its
    # SHA1 sum and include it in the blueprint.
    try:
        tar = tarfile.open('tmp.tar', 'w')
        tar.add(tmpname, '.')
    except OSError:
        return
    finally:
        tar.close()
    sha1 = hashlib.sha1()
    f = open('tmp.tar', 'r')
    [sha1.update(buf) for buf in iter(lambda: f.read(4096), '')]
    f.close()
    tarname = '{0}.tar'.format(sha1.hexdigest())
    shutil.move('tmp.tar', os.path.join(old_cwd, tarname))
    b.add_source(dirname, tarname)


def sources(b):
    logging.info('searching for software built from source')
    for pathname, negate in ignore.cache['source']:
        if negate and os.path.isdir(pathname) and not ignore.source(pathname):

            # Note before creating a working directory within pathname what
            # it's atime and mtime should be.
            s = os.lstat(pathname)

            # Create a working directory within pathname to avoid potential
            # EXDEV when creating the shallow copy and tarball.
            try:
                with context_managers.mkdtemp(pathname) as c:

                    # Restore the parent of the working directory to its
                    # original atime and mtime, as if pretending the working
                    # directory never actually existed.
                    os.utime(pathname, (s.st_atime, s.st_mtime))

                    # Create the shallow copy and possibly tarball of the
                    # relevant parts of pathname.
                    _source(b, pathname, c.cwd)

                # Once more restore the atime and mtime after the working
                # directory is destroyed.
                os.utime(pathname, (s.st_atime, s.st_mtime))

            # If creating the temporary directory fails, bail with a warning.
            except OSError as e:
                logging.warning('{0} caused {1} - try running as root'.
                                format(pathname, errno.errorcode[e.errno]))

    if 0 < len(b.sources):
        b.arch = util.arch()
