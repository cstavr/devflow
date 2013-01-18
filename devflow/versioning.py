#!/usr/bin/env python
#
# Copyright (C) 2010, 2011, 2012 GRNET S.A. All rights reserved.
#
# Redistribution and use in source and binary forms, with or
# without modification, are permitted provided that the following
# conditions are met:
#
#   1. Redistributions of source code must retain the above
#      copyright notice, this list of conditions and the following
#      disclaimer.
#
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY GRNET S.A. ``AS IS'' AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL GRNET S.A OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and
# documentation are those of the authors and should not be
# interpreted as representing official policies, either expressed
# or implied, of GRNET S.A.


import os
import re
import sys
import pprint
import subprocess
import git

from distutils import log
from collections import namedtuple


# Branch types:
# builds_snapshot: Whether the branch can produce snapshot builds
# builds_release: Whether the branch can produce release builds
# versioned: Whether the name of the branch defines a specific version
# allowed_version_re: A regular expression describing allowed values for
#                     base_version in this branch
branch_type = namedtuple("branch_type", ["builds_snapshot", "builds_release",
                                         "versioned", "allowed_version_re"])
VERSION_RE = "[0-9]+\.[0-9]+(\.[0-9]+)*"
BRANCH_TYPES = {
    "feature": branch_type(True, False, False, "^%snext$" % VERSION_RE),
    "develop": branch_type(True, False, False, "^%snext$" % VERSION_RE),
    "release": branch_type(True, True, True,
                           "^(?P<bverstr>%s)rc[1-9][0-9]*$" % VERSION_RE),
    "master": branch_type(False, True, False,
                          "^%s$" % VERSION_RE),
    "hotfix": branch_type(True, True, True,
                          "^(?P<bverstr>^%s\.[1-9][0-9]*)$" % VERSION_RE)}
BASE_VERSION_FILE = "version"


def get_commit_id(commit, current_branch):
    """Return the commit ID

    If the commit is a 'merge' commit, and one of the parents is a
    debian branch we return a compination of the parents commits.

    """
    def short_id(commit):
        return commit.hexsha[0:7]

    parents = commit.parents
    cur_br_name = current_branch.name
    if len(parents) == 1:
        return short_id(commit)
    elif len(parents) == 2:
        if cur_br_name.startswith("debian-") or cur_br_name == "debian":
            pr1, pr2 = parents
            return short_id(pr1) + "-" + short_id(pr2)
        else:
            return short_id(commit)
    else:
        raise RuntimeError("Commit %s has more than 2 parents!" % commit)


def vcs_info():
    """
    Return current git HEAD commit information.

    Returns a tuple containing
        - branch name
        - commit id
        - commit count
        - git describe output
        - path of git toplevel directory

    """
    try:
        repo = git.Repo(".")
        branch = repo.head.reference
        revid = get_commit_id(branch.commit, branch)
        revno = len(list(repo.iter_commits()))
        toplevel = repo.working_dir
    except git.InvalidGitRepositoryError:
        log.error("Could not retrieve git information. " +
                  "Current directory not a git repository?")
        return None

    info = namedtuple("vcs_info", ["branch", "revid", "revno",
                                   "toplevel"])

    return info(branch=branch.name, revid=revid, revno=revno,
                toplevel=toplevel)


def base_version(vcs_info):
    """Determine the base version from a file in the repository"""

    f = open(os.path.join(vcs_info.toplevel, BASE_VERSION_FILE))
    lines = [l.strip() for l in f.readlines()]
    l = [l for l in lines if not l.startswith("#")]
    if len(l) != 1:
        raise ValueError("File '%s' should contain a single non-comment line.")
    return l[0]


def build_mode():
    """Determine the build mode from the value of $GITFLOW_BUILD_MODE"""
    try:
        mode = os.environ["GITFLOW_BUILD_MODE"]
        assert mode == "release" or mode == "snapshot"
    except KeyError:
        raise ValueError("GITFLOW_BUILD_MODE environment variable is not set."
                         " Set this variable to 'release' or 'snapshot'")
    except AssertionError:
        raise ValueError("GITFLOW_BUILD_MODE environment variable must be"
                         " 'release' or 'snapshot'")
    return mode


def normalize_branch_name(branch_name):
    """Normalize branch name by removing debian- if exists"""
    brnorm = branch_name
    if brnorm == "debian":
        brnorm = "debian-master"
    # If it's a debian branch, ignore starting "debian-"
    if brnorm.startswith("debian-"):
        brnorm = brnorm.replace("debian-", "", 1)
    return brnorm


def get_branch_type(branch_name):
    """Extract the type from a branch name"""
    if "-" in branch_name:
        btypestr = branch_name.split("-")[0]
    else:
        btypestr = branch_name
    return btypestr


def python_version(base_version, vcs_info, mode):
    """Generate a Python distribution version following devtools conventions.

    This helper generates a Python distribution version from a repository
    commit, following devtools conventions. The input data are:
        * base_version: a base version number, presumably stored in text file
          inside the repository, e.g., /version
        * vcs_info: vcs information: current branch name and revision no
        * mode: "snapshot", or "release"

    This helper assumes a git branching model following:
    http://nvie.com/posts/a-successful-git-branching-model/

    with 'master', 'develop', 'release-X', 'hotfix-X' and 'feature-X' branches.

    General rules:
    a) any repository commit can get as a Python version
    b) a version is generated either in 'release' or in 'snapshot' mode
    c) the choice of mode depends on the branch, see following table.

    A python version is of the form A_NNN,
    where A: X.Y.Z{,next,rcW} and NNN: a revision number for the commit,
    as returned by vcs_info().

    For every combination of branch and mode, releases are numbered as follows:

    BRANCH:  /  MODE: snapshot        release
    --------          ------------------------------
    feature           0.14next_150    N/A
    develop           0.14next_151    N/A
    release           0.14rc2_249     0.14rc2
    master            N/A             0.14
    hotfix            0.14.1rc6_121   0.14.1rc6
                      N/A             0.14.1

    The suffix 'next' in a version name is used to denote the upcoming version,
    the one being under development in the develop and release branches.
    Version '0.14next' is the version following 0.14, and only lives on the
    develop and feature branches.

    The suffix 'rc' is used to denote release candidates. 'rc' versions live
    only in release and hotfix branches.

    Suffixes 'next' and 'rc' have been chosen to ensure proper ordering
    according to setuptools rules:

        http://www.python.org/dev/peps/pep-0386/#setuptools

    Every branch uses a value for A so that all releases are ordered based
    on the branch they came from, so:

    So
        0.13next < 0.14rcW < 0.14 < 0.14next < 0.14.1

    and

    >>> V("0.14next") > V("0.14")
    True
    >>> V("0.14next") > V("0.14rc7")
    True
    >>> V("0.14next") > V("0.14.1")
    False
    >>> V("0.14rc6") > V("0.14")
    False
    >>> V("0.14.2rc6") > V("0.14.1")
    True

    The value for _NNN is chosen based of the revision number of the specific
    commit. It is used to ensure ascending ordering of consecutive releases
    from the same branch. Every version of the form A_NNN comes *before*
    than A: All snapshots are ordered so they come before the corresponding
    release.

    So
        0.14next_* < 0.14
        0.14.1_* < 0.14.1
        etc

    and

    >>> V("0.14next_150") < V("0.14next")
    True
    >>> V("0.14.1next_150") < V("0.14.1next")
    True
    >>> V("0.14.1_149") < V("0.14.1")
    True
    >>> V("0.14.1_149") < V("0.14.1_150")
    True

    Combining both of the above, we get
       0.13next_* < 0.13next < 0.14rcW_* < 0.14rcW < 0.14_* < 0.14
       < 0.14next_* < 0.14next < 0.14.1_* < 0.14.1

    and

    >>> V("0.13next_102") < V("0.13next")
    True
    >>> V("0.13next") < V("0.14rc5_120")
    True
    >>> V("0.14rc3_120") < V("0.14rc3")
    True
    >>> V("0.14rc3") < V("0.14_1")
    True
    >>> V("0.14_120") < V("0.14")
    True
    >>> V("0.14") < V("0.14next_20")
    True
    >>> V("0.14next_20") < V("0.14next")
    True

    Note: one of the tests above fails because of constraints in the way
    setuptools parses version numbers. It does not affect us because the
    specific version format that triggers the problem is not contained in the
    table showing allowed branch / mode combinations, above.


    """

    branch = vcs_info.branch


    brnorm = normalize_branch_name(branch)
    btypestr = get_branch_type(brnorm)

    try:
        btype = BRANCH_TYPES[btypestr]
    except KeyError:
        allowed_branches = ", ".join(x for x in BRANCH_TYPES.keys())
        raise ValueError("Malformed branch name '%s', cannot classify as one "
                         "of %s" % (btypestr, allowed_branches))

    if btype.versioned:
        try:
            bverstr = brnorm.split("-")[1]
        except IndexError:
            # No version
            raise ValueError("Branch name '%s' should contain version" %
                             branch)

        # Check that version is well-formed
        if not re.match(VERSION_RE, bverstr):
            raise ValueError("Malformed version '%s' in branch name '%s'" %
                             (bverstr, branch))

    m = re.match(btype.allowed_version_re, base_version)
    if not m or (btype.versioned and m.groupdict()["bverstr"] != bverstr):
        raise ValueError("Base version '%s' unsuitable for branch name '%s'" %
                         (base_version, branch))

    if mode not in ["snapshot", "release"]:
        raise ValueError("Specified mode '%s' should be one of 'snapshot' or "
                         "'release'" % mode)
    snap = (mode == "snapshot")

    if ((snap and not btype.builds_snapshot) or
        (not snap and not btype.builds_release)):
        raise ValueError("Invalid mode '%s' in branch type '%s'" %
                         (mode, btypestr))

    if snap:
        v = "%s_%d_%s" % (base_version, vcs_info.revno, vcs_info.revid)
    else:
        v = base_version
    return v


def debian_version_from_python_version(pyver):
    """Generate a debian package version from a Python version.

    This helper generates a Debian package version from a Python version,
    following devtools conventions.

    Debian sorts version strings differently compared to setuptools:
    http://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-Version

    Initial tests:

    >>> debian_version("3") < debian_version("6")
    True
    >>> debian_version("3") < debian_version("2")
    False
    >>> debian_version("1") == debian_version("1")
    True
    >>> debian_version("1") != debian_version("1")
    False
    >>> debian_version("1") >= debian_version("1")
    True
    >>> debian_version("1") <= debian_version("1")
    True

    This helper defines a 1-1 mapping between Python and Debian versions,
    with the same ordering.

    Debian versions are ordered in the same way as Python versions:

    >>> D("0.14next") > D("0.14")
    True
    >>> D("0.14next") > D("0.14rc7")
    True
    >>> D("0.14next") > D("0.14.1")
    False
    >>> D("0.14rc6") > D("0.14")
    False
    >>> D("0.14.2rc6") > D("0.14.1")
    True

    and

    >>> D("0.14next_150") < D("0.14next")
    True
    >>> D("0.14.1next_150") < D("0.14.1next")
    True
    >>> D("0.14.1_149") < D("0.14.1")
    True
    >>> D("0.14.1_149") < D("0.14.1_150")
    True

    and

    >>> D("0.13next_102") < D("0.13next")
    True
    >>> D("0.13next") < D("0.14rc5_120")
    True
    >>> D("0.14rc3_120") < D("0.14rc3")
    True
    >>> D("0.14rc3") < D("0.14_1")
    True
    >>> D("0.14_120") < D("0.14")
    True
    >>> D("0.14") < D("0.14next_20")
    True
    >>> D("0.14next_20") < D("0.14next")
    True

    """
    return pyver.replace("_", "~").replace("rc", "~rc") + "-1"


def get_python_version():
    v = vcs_info()
    b = base_version(v)
    mode = build_mode()
    return python_version(b, v, mode)


def debian_version(base_version, vcs_info, mode):
    p = python_version(base_version, vcs_info, mode)
    return debian_version_from_python_version(p)


def get_debian_version():
    v = vcs_info()
    b = base_version(v)
    mode = build_mode()
    return debian_version(b, v, mode)


def user_info():
    import getpass
    import socket
    return "%s@%s" % (getpass.getuser(), socket.getfqdn())


def update_version(module, name="version", root="."):
    """
    Generate or replace version.py as a submodule of `module`.

    This is a helper to generate/replace a version.py file containing version
    information as a submodule of passed `module`.

    """

    paths = [root] + module.split(".") + ["%s.py" % name]
    module_filename = os.path.join(*paths)

    v = vcs_info()
    if not v:
        # Return early if not in development environment
        log.error("Can not compute version outside of a git repository."
                  " Will not update %s version file" % module_filename)
        return
    b = base_version(v)
    mode = build_mode()
    version = python_version(b, v, mode)
    content = """
__version__ = "%(version)s"
__version_info__ = %(version_info)s
__version_vcs_info__ = %(vcs_info)s
__version_user_info__ = "%(user_info)s"
""" % dict(version=version, version_info=version.split("."),
               vcs_info=pprint.PrettyPrinter().pformat(dict(v._asdict())),
               user_info=user_info())

    module_file = file(module_filename, "w+")
    module_file.write(content)
    module_file.close()
    return module_filename


def bump_version_main():
    try:
        version = sys.argv[1]
        bump_version(version)
    except IndexError:
        sys.stdout.write("Give me a version %s!\n")
        sys.stdout.write("usage: %s version\n" % sys.argv[0])


def bump_version(new_version):
    """Set new base version to base version file and commit"""
    v = vcs_info()
    mode = build_mode()

    # Check that new base version is valid
    python_version(new_version, v, mode)

    repo = git.Repo(".")
    toplevel = repo.working_dir

    old_version = base_version(v)
    sys.stdout.write("Current base version is '%s'\n" % old_version)

    version_file = toplevel + "/version"
    sys.stdout.write("Updating version file %s from version '%s' to '%s'\n"
                     % (version_file, old_version, new_version))

    f = open(version_file, 'rw+')
    lines = f.readlines()
    for i in range(0, len(lines)):
        if not lines[i].startswith("#"):
                lines[i] = lines[i].replace(old_version, new_version)
    f.seek(0)
    f.truncate(0)
    f.writelines(lines)
    f.close()

    repo.git.add(version_file)
    repo.git.commit(m="Bump version")
    sys.stdout.write("Update version file and commited\n")


def main():
    v = vcs_info()
    b = base_version(v)
    mode = build_mode()

    try:
        arg = sys.argv[1]
        assert arg == "python" or arg == "debian"
    except IndexError:
        raise ValueError("A single argument, 'python' or 'debian is required")

    if arg == "python":
        print python_version(b, v, mode)
    elif arg == "debian":
        print debian_version(b, v, mode)

if __name__ == "__main__":
    sys.exit(main())
