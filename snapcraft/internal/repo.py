# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2015-2016 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import contextlib
import fileinput
import glob
import hashlib
import itertools
import logging
import os
import platform
import re
import shutil
import stat
import string
import subprocess
import sys
import urllib
import urllib.request

import apt
from xml.etree import ElementTree

import snapcraft
from snapcraft import file_utils
from snapcraft.internal import (
    cache,
)
from snapcraft.internal.errors import MissingCommandError
from snapcraft.internal.indicators import is_dumb_terminal


_BIN_PATHS = (
    'bin',
    'sbin',
    'usr/bin',
    'usr/sbin',
)

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = \
    '''deb http://${prefix}.ubuntu.com/${suffix}/ ${release} main restricted
deb http://${prefix}.ubuntu.com/${suffix}/ ${release}-updates main restricted
deb http://${prefix}.ubuntu.com/${suffix}/ ${release} universe
deb http://${prefix}.ubuntu.com/${suffix}/ ${release}-updates universe
deb http://${prefix}.ubuntu.com/${suffix}/ ${release} multiverse
deb http://${prefix}.ubuntu.com/${suffix}/ ${release}-updates multiverse
deb http://${security}.ubuntu.com/${suffix} ${release}-security main restricted
deb http://${security}.ubuntu.com/${suffix} ${release}-security universe
deb http://${security}.ubuntu.com/${suffix} ${release}-security multiverse
'''
_GEOIP_SERVER = "http://geoip.ubuntu.com/lookup"


def is_package_installed(package):
    """Return True if a package is installed on the system.

    :param str package: the deb package to query for.
    :returns: True if the package is installed, False if not.
    """
    with apt.Cache() as apt_cache:
        return apt_cache[package].installed


def install_build_packages(packages):
    unique_packages = set(packages)
    new_packages = []
    with apt.Cache() as apt_cache:
        for pkg in unique_packages:
            try:
                if not apt_cache[pkg].installed:
                    new_packages.append(pkg)
            except KeyError as e:
                raise EnvironmentError(
                    'Could not find a required package in '
                    '\'build-packages\': {}'.format(str(e)))
    if new_packages:
        new_packages.sort()
        logger.info(
            'Installing build dependencies: %s', ' '.join(new_packages))
        env = os.environ.copy()
        env.update({
            'DEBIAN_FRONTEND': 'noninteractive',
            'DEBCONF_NONINTERACTIVE_SEEN': 'true',
        })

        apt_command = ['sudo', 'apt-get',
                       '--no-install-recommends', '-y']
        if not is_dumb_terminal():
            apt_command.extend(['-o', 'Dpkg::Progress-Fancy=1'])
        apt_command.append('install')

        subprocess.check_call(apt_command + new_packages, env=env)

        try:
            subprocess.check_call(['sudo', 'apt-mark', 'auto'] +
                                  new_packages, env=env)
        except subprocess.CalledProcessError as e:
            logger.warning(
                'Impossible to mark packages as auto-installed: {}'
                .format(e))


def get_packages_for_source_type(source_type):
    """Return a list with required packages to handle the source_type.

    :param source: the snapcraft source type
    """
    if source_type == 'bzr':
        packages = 'bzr'
    elif source_type == 'git':
        packages = 'git'
    elif source_type == 'tar':
        packages = 'tar'
    elif source_type == 'hg' or source_type == 'mercurial':
        packages = 'mercurial'
    elif source_type == 'subversion' or source_type == 'svn':
        packages = 'subversion'
    else:
        packages = []

    return packages


class PackageNotFoundError(Exception):

    @property
    def message(self):
        message = 'The Ubuntu package {!r} was not found.'.format(
            self.package_name)
        # If the package was multiarch, try to help.
        if ':' in self.package_name:
            (name, arch) = self.package_name.split(':', 2)
            if arch:
                message += (
                    '\nYou may need to add support for this architecture with '
                    "'dpkg --add-architecture {}'.".format(arch))
        return message

    def __init__(self, package_name):
        self.package_name = package_name


class UnpackError(Exception):

    @property
    def message(self):
        return 'Error while provisioning "{}"'.format(self.package_name)

    def __init__(self, package_name):
        self.package_name = package_name


class _AptCache:

    def __init__(self, deb_arch, *, sources_list=None, use_geoip=False):
        self._deb_arch = deb_arch
        self._sources_list = sources_list
        self._use_geoip = use_geoip

    def _setup_apt(self, cache_dir):
        # Do not install recommends
        apt.apt_pkg.config.set('Apt::Install-Recommends', 'False')

        # Methods and solvers dir for when in the SNAP
        if os.getenv('SNAP'):
            snap_dir = os.getenv('SNAP')
            apt_dir = os.path.join(snap_dir, 'apt')
            apt.apt_pkg.config.set('Dir', apt_dir)
            # yes apt is broken like that we need to append os.path.sep
            apt.apt_pkg.config.set('Dir::Bin::methods',
                                   apt_dir + os.path.sep)
            apt.apt_pkg.config.set('Dir::Bin::solvers::',
                                   apt_dir + os.path.sep)
            apt_key_path = os.path.join(apt_dir, 'apt-key')
            apt.apt_pkg.config.set('Dir::Bin::apt-key', apt_key_path)
            gpgv_path = os.path.join(snap_dir, 'bin', 'gpgv')
            apt.apt_pkg.config.set('Apt::Key::gpgvcommand', gpgv_path)
            apt.apt_pkg.config.set('Dir::Etc::Trusted',
                                   '/etc/apt/trusted.gpg')
            apt.apt_pkg.config.set('Dir::Etc::TrustedParts',
                                   '/etc/apt/trusted.gpg.d/')

        # Make sure we always use the system GPG configuration, even with
        # apt.Cache(rootdir).
        for key in 'Dir::Etc::Trusted', 'Dir::Etc::TrustedParts':
            apt.apt_pkg.config.set(key, apt.apt_pkg.config.find_file(key))

        # Clear up apt's Post-Invoke-Success as we are not running
        # on the system.
        apt.apt_pkg.config.clear('APT::Update::Post-Invoke-Success')

        self.progress = apt.progress.text.AcquireProgress()
        if is_dumb_terminal():
            # Make output more suitable for logging.
            self.progress.pulse = lambda owner: True
            self.progress._width = 0

        sources_list_file = os.path.join(
            cache_dir, 'etc', 'apt', 'sources.list')

        os.makedirs(os.path.dirname(sources_list_file), exist_ok=True)
        with open(sources_list_file, 'w') as f:
            f.write(self._collected_sources_list())

        # dpkg also needs to be in the rootdir in order to support multiarch
        # (apt calls dpkg --print-foreign-architectures).
        dpkg_path = shutil.which('dpkg')
        if dpkg_path:
            # Symlink it into place
            destination = os.path.join(cache_dir, dpkg_path[1:])
            if not os.path.exists(destination):
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                os.symlink(dpkg_path, destination)
        else:
            logger.warning(
                "Cannot find 'dpkg' command needed to support multiarch")

        apt_cache = apt.Cache(rootdir=cache_dir, memonly=True)
        apt_cache.update(fetch_progress=self.progress,
                         sources_list=sources_list_file)

        return apt_cache

    @contextlib.contextmanager
    def archive(self, cache_dir):
        try:
            apt_cache = self._setup_apt(cache_dir)
            apt_cache.open()

            try:
                yield apt_cache
            finally:
                apt_cache.close()
        except Exception as e:
            logger.debug('Exception occured: {!r}'.format(e))
            raise e

    def sources_digest(self):
        return hashlib.sha384(self._collected_sources_list().encode(
            sys.getfilesystemencoding())).hexdigest()

    def _collected_sources_list(self):
        if self._use_geoip or self._sources_list:
            release = platform.linux_distribution()[2]
            return _format_sources_list(
                self._sources_list, deb_arch=self._deb_arch,
                use_geoip=self._use_geoip, release=release)

        return _get_local_sources_list()


class Ubuntu:

    def __init__(self, rootdir, recommends=False, sources=None,
                 project_options=None):
        self._downloaddir = os.path.join(rootdir, 'download')
        self._rootdir = rootdir
        os.makedirs(self._downloaddir, exist_ok=True)

        if not project_options:
            project_options = snapcraft.ProjectOptions()

        self._apt = _AptCache(
            project_options.deb_arch, sources_list=sources,
            use_geoip=project_options.use_geoip)

        self._cache = cache.AptStagePackageCache(
            sources_digest=self._apt.sources_digest())

    def is_valid(self, package_name):
        with self._apt.archive(self._cache.base_dir) as apt_cache:
            return package_name in apt_cache

    def get(self, package_names):
        with self._apt.archive(self._cache.base_dir) as apt_cache:
            self._mark_install(apt_cache, package_names)
            self._filter_base_packages(apt_cache, package_names)
            return self._get(apt_cache)

    def _mark_install(self, apt_cache, package_names):
        for name in package_names:
            logger.debug('Marking {!r} (and its dependencies) to be '
                         'fetched'.format(name))
            name_arch, version = _get_pkg_name_parts(name)
            try:
                if version:
                    _set_pkg_version(apt_cache[name_arch], version)
                apt_cache[name_arch].mark_install()
            except KeyError:
                raise PackageNotFoundError(name)

    def _filter_base_packages(self, apt_cache, package_names):
        manifest_dep_names = self._manifest_dep_names(apt_cache)

        skipped_essential = []
        skipped_blacklisted = []

        # unmark some base packages here
        # note that this will break the consistency check inside apt_cache
        # (apt_cache.broken_count will be > 0)
        # but that is ok as it was consistent before we excluded
        # these base package
        for pkg in apt_cache:
            # those should be already on each system, it also prevents
            # diving into downloading libc6
            if (pkg.candidate.priority in 'essential' and
               pkg.name not in package_names):
                skipped_essential.append(pkg.name)
                pkg.mark_keep()
                continue
            if (pkg.name in manifest_dep_names and
                    pkg.name not in package_names):
                skipped_blacklisted.append(pkg.name)
                pkg.mark_keep()
                continue

        if skipped_essential:
            logger.debug('Skipping priority essential packages: '
                         '{!r}'.format(skipped_essential))
        if skipped_blacklisted:
            logger.debug('Skipping blacklisted from manifest packages: '
                         '{!r}'.format(skipped_blacklisted))

    def _get(self, apt_cache):
        # Ideally we'd use apt.Cache().fetch_archives() here, but it seems to
        # mangle some package names on disk such that we can't match it up to
        # the archive later. We could get around this a few different ways:
        #
        # 1. Store each stage package in the cache named by a hash instead of
        #    its name from the archive.
        # 2. Download packages in a different manner.
        #
        # In the end, (2) was chosen for minimal overhead and a simpler cache
        # implementation. So we're using fetch_binary() here instead.
        # Downloading each package individually has the drawback of witholding
        # any clue of how long the whole pulling process will take, but that's
        # something we'll have to live with.
        pkg_list = []
        for package in apt_cache.get_changes():
            pkg_list.append(str(package.candidate))
            source = package.candidate.fetch_binary(
                self._cache.packages_dir, progress=self._apt.progress)
            destination = os.path.join(
                self._downloaddir, os.path.basename(source))
            with contextlib.suppress(FileNotFoundError):
                os.remove(destination)
            file_utils.link_or_copy(source, destination)

        return pkg_list

    def unpack(self, rootdir):
        pkgs_abs_path = glob.glob(os.path.join(self._downloaddir, '*.deb'))
        for pkg in pkgs_abs_path:
            # TODO needs elegance and error control
            try:
                subprocess.check_call(['dpkg-deb', '--extract', pkg, rootdir])
            except subprocess.CalledProcessError:
                raise UnpackError(pkg)

        _fix_artifacts(rootdir)
        _fix_xml_tools(rootdir)
        _fix_shebangs(rootdir)

    def _manifest_dep_names(self, apt_cache):
        manifest_dep_names = set()

        with open(os.path.abspath(os.path.join(__file__, '..',
                                               'manifest.txt'))) as f:
            for line in f:
                pkg = line.strip()
                if pkg in apt_cache:
                    manifest_dep_names.add(pkg)

        return manifest_dep_names


def _get_local_sources_list():
    sources_list = glob.glob('/etc/apt/sources.list.d/*.list')
    sources_list.append('/etc/apt/sources.list')

    sources = ''
    for source in sources_list:
        with open(source) as f:
            sources += f.read()

    return sources


def _get_geoip_country_code_prefix():
    try:
        with urllib.request.urlopen(_GEOIP_SERVER) as f:
            xml_data = f.read()
        et = ElementTree.fromstring(xml_data)
        cc = et.find("CountryCode")
        if cc is None:
            return ""
        return cc.text.lower()
    except (ElementTree.ParseError, urllib.error.URLError):
        pass
    return ''


def _format_sources_list(sources_list, *,
                         deb_arch, use_geoip=False, release='xenial'):
    if not sources_list:
        sources_list = _DEFAULT_SOURCES

    if deb_arch in ('amd64', 'i386'):
        if use_geoip:
            geoip_prefix = _get_geoip_country_code_prefix()
            prefix = '{}.archive'.format(geoip_prefix)
        else:
            prefix = 'archive'
        suffix = 'ubuntu'
        security = 'security'
    else:
        prefix = 'ports'
        suffix = 'ubuntu-ports'
        security = 'ports'

    return string.Template(sources_list).substitute({
        'prefix': prefix,
        'release': release,
        'suffix': suffix,
        'security': security,
    })


def fix_pkg_config(root, pkg_config_file, prefix_trim=None):
    """Opens a pkg_config_file and prefixes the prefix with root."""
    pattern_trim = None
    if prefix_trim:
        pattern_trim = re.compile(
            '^prefix={}(?P<prefix>.*)'.format(prefix_trim))
    pattern = re.compile('^prefix=(?P<prefix>.*)')

    with fileinput.input(pkg_config_file, inplace=True) as input_file:
        for line in input_file:
            match = pattern.search(line)
            if prefix_trim:
                match_trim = pattern_trim.search(line)
            if prefix_trim and match_trim:
                print('prefix={}{}'.format(root, match_trim.group('prefix')))
            elif match:
                print('prefix={}{}'.format(root, match.group('prefix')))
            else:
                print(line, end='')


def _fix_artifacts(debdir):
    '''
    Sometimes debs will contain absolute symlinks (e.g. if the relative
    path would go all the way to root, they just do absolute).  We can't
    have that, so instead clean those absolute symlinks.

    Some unpacked items will also contain suid binaries which we do not want in
    the resulting snap.
    '''
    for root, dirs, files in os.walk(debdir):
        # Symlinks to directories will be in dirs, while symlinks to
        # non-directories will be in files.
        for entry in itertools.chain(files, dirs):
            path = os.path.join(root, entry)
            if os.path.islink(path) and os.path.isabs(os.readlink(path)):
                _fix_symlink(path, debdir, root)
            elif os.path.exists(path):
                _fix_filemode(path)

            if path.endswith('.pc') and not os.path.islink(path):
                fix_pkg_config(debdir, path)


def _fix_xml_tools(root):
    xml2_config_path = os.path.join(root, 'usr', 'bin', 'xml2-config')
    with contextlib.suppress(FileNotFoundError):
        file_utils.search_and_replace_contents(
            xml2_config_path, re.compile(r'prefix=/usr'),
            'prefix={}/usr'.format(root))

    xslt_config_path = os.path.join(root, 'usr', 'bin', 'xslt-config')
    with contextlib.suppress(FileNotFoundError):
        file_utils.search_and_replace_contents(
            xslt_config_path, re.compile(r'prefix=/usr'),
            'prefix={}/usr'.format(root))


def _fix_symlink(path, debdir, root):
    target = os.readlink(path)
    debdir_target = os.path.join(debdir, os.readlink(path)[1:])

    if target in get_pkg_libs('libc6'):
        logger.debug("Not fixing symlink {!r}: it's pointing to libc".format(
            target))
        return
    if (not os.path.exists(debdir_target) and not
            _try_copy_local(path, debdir_target)):
        return
    os.remove(path)
    os.symlink(os.path.relpath(debdir_target, root), path)


def _fix_filemode(path):
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o4000 or mode & 0o2000:
        logger.warning('Removing suid/guid from {}'.format(path))
        os.chmod(path, mode & 0o1777)


def _fix_shebangs(path):
    """Changes hard coded shebangs for files in _BIN_PATHS to use env."""
    paths = [p for p in _BIN_PATHS if os.path.exists(os.path.join(path, p))]
    for p in [os.path.join(path, p) for p in paths]:
        file_utils.replace_in_file(p, re.compile(r''),
                                   re.compile(r'#!.*python\n'),
                                   r'#!/usr/bin/env python\n')


def _try_copy_local(path, target):
    real_path = os.path.realpath(path)
    if os.path.exists(real_path):
        logger.warning(
            'Copying needed target link from the system {}'.format(real_path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.readlink(path), target)
        return True
    else:
        logger.warning(
            '{} will be a dangling symlink'.format(path))
        return False


def check_for_command(command):
    if not shutil.which(command):
        raise MissingCommandError([command])


def _get_pkg_name_parts(pkg_name):
    """Break package name into base parts"""

    name = pkg_name
    version = None
    with contextlib.suppress(ValueError):
        name, version = pkg_name.split('=')

    return name, version


def _set_pkg_version(pkg, version):
    """Set cadidate version to a specific version if available"""
    if version in pkg.versions:
        version = pkg.versions.get(version)
        pkg.candidate = version
    else:
        raise PackageNotFoundError('{}={}'.format(pkg.name, version))


_lib_list = dict()


def get_pkg_libs(pkg_name):
    """Obtain list of libraries contained within a Debian package.

    :param str pkg_name: Name of the package.

    :return: Set of files in the package with 'lib' in the name. This will
             include directories.
    :rtype: set

    Note that this will be slow the first time it's called for a given package
    name, but the list is cached, so subsequent calls for the same package will
    be fast.
    """

    global _lib_list
    if pkg_name not in _lib_list:
        # No need to use common.run here, as nothing depends upon the snap's
        # build environment.
        output = subprocess.check_output(['dpkg', '-L', pkg_name]).decode(
            sys.getfilesystemencoding()).strip().split()
        _lib_list[pkg_name] = {i for i in output if 'lib' in i}

    return _lib_list[pkg_name].copy()
