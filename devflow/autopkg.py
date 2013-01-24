# Copyright 2012, 2013 GRNET S.A. All rights reserved.
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

import git
import os
import sys
from optparse import OptionParser
from collections import namedtuple
from sh import mktemp, cd, rm, git_dch, python

from devflow import versioning

try:
    from colors import red, green
except ImportError:
    red = lambda x: x
    green = lambda x: x

print_red = lambda x: sys.stdout.write(red(x) + "\n")
print_green = lambda x: sys.stdout.write(green(x) + "\n")

AVAILABLE_MODES = ["release", "snapshot"]

branch_type = namedtuple("branch_type", ["default_debian_branch"])
BRANCH_TYPES = {
    "feature": branch_type("debian-develop"),
    "develop": branch_type("debian-develop"),
    "release": branch_type("debian-develop"),
    "master": branch_type("debian"),
    "hotfix": branch_type("debian")}


DESCRIPTION = """Tool for automatical build of debian packages.

%(prog)s is a helper script for automatic build of debian packages from
repositories that follow the `git flow` development model
<http://nvie.com/posts/a-successful-git-branching-model/>.

This script must run from inside a clean git repository and will perform the
following steps:
    * Clone your repository to a temporary directory
    * Merge the current branch with the corresponding debian branch
    * Compute the version of the new package and update the python
      version files
    * Create a new entry in debian/changelog, using `git-dch`
    * Create the debian packages, using `git-buildpackage`
    * Tag the appropriate branches if in `release` mode

%(prog)s will work with the packages that are declared in `autopkg.conf`
file, which must exist in the toplevel directory of the git repository.

"""


def print_help(prog):
    print DESCRIPTION % {"prog": prog}


def main():
    from devflow.version import __version__
    parser = OptionParser(usage="usage: %prog [options] mode",
                          version="devflow %s" % __version__,
                          add_help_option=False)
    parser.add_option("-h", "--help",
                      action="store_true",
                      default=False,
                      help="show this help message")
    parser.add_option("-k", "--keep-repo",
                      action="store_true",
                      dest="keep_repo",
                      default=False,
                      help="Do not delete the cloned repository")
    parser.add_option("-b", "--build-dir",
                      dest="build_dir",
                      default=None,
                      help="Directory to store created pacakges")
    parser.add_option("-r", "--repo-dir",
                      dest="repo_dir",
                      default=None,
                      help="Directory to clone repository")
    parser.add_option("-d", "--dirty",
                      dest="force_dirty",
                      default=False,
                      action="store_true",
                      help="Do not check if working directory is dirty")
    parser.add_option("-c", "--config-file",
                      dest="config_file",
                      help="Override default configuration file")

    (options, args) = parser.parse_args()

    if options.help:
        print_help(parser.get_prog_name())
        parser.print_help()
        return

    # Get build mode
    try:
        mode = args[0]
    except IndexError:
        raise ValueError("Mode argument is mandatory. Usage: %s"
                         % parser.usage)
    if mode not in AVAILABLE_MODES:
        raise ValueError(red("Invalid argument! Mode must be one: %s"
                         % ", ".join(AVAILABLE_MODES)))

    os.environ["GITFLOW_BUILD_MODE"] = mode

    # Load the repository
    try:
        original_repo = git.Repo(".")
    except git.git.InvalidGitRepositoryError:
        raise RuntimeError(red("Current directory is not git repository."))

    # Check that repository is clean
    toplevel = original_repo.working_dir
    if original_repo.is_dirty() and not options.force_dirty:
        raise RuntimeError(red("Repository %s is dirty." % toplevel))

    # Get packages from configuration file
    config_file = options.config_file or os.path.join(toplevel, "autopkg.conf")
    packages = get_packages_to_build(config_file)
    if packages:
        print_green("Will build the following packages:\n"
                    "\n".join(packages))
    else:
        raise RuntimeError("Configuration file is empty."
                           " No packages to build.")

    # Clone the repo
    repo_dir = options.repo_dir
    if not repo_dir:
        repo_dir = create_temp_directory("df-repo")
        print_green("Created temporary directory '%s' for the cloned repo."
                    % repo_dir)

    repo = original_repo.clone(repo_dir)
    print_green("Cloned current repository to '%s'." % repo_dir)

    reflog_hexsha = repo.head.log()[-1].newhexsha
    print "Latest Reflog entry is %s" % reflog_hexsha

    branch = repo.head.reference.name
    allowed_branches = ", ".join(x for x in BRANCH_TYPES.keys())
    if branch.split('-')[0] not in allowed_branches:
        raise ValueError("Malformed branch name '%s', cannot classify as"
                         " one of %s" % (branch, allowed_branches))

    brnorm = versioning.normalize_branch_name(branch)
    btypestr = versioning.get_branch_type(brnorm)

    # Find the debian branch, and create it if does not exist
    debian_branch = "debian-" + brnorm
    origin_debian = "origin/" + debian_branch
    if not origin_debian in repo.references:
        # Get default debian branch
        try:
            default_debian = BRANCH_TYPES[btypestr].default_debian_branch
            origin_debian = "origin/" + default_debian
        except KeyError:
            allowed_branches = ", ".join(x for x in BRANCH_TYPES.keys())
            raise ValueError("Malformed branch name '%s', cannot classify as"
                             " one of %s" % (btypestr, allowed_branches))

    repo.git.branch("--track", debian_branch, origin_debian)
    print_green("Created branch '%s' to track '%s'" % (debian_branch,
                origin_debian))

    # Go to debian branch
    repo.git.checkout(debian_branch)
    print_green("Changed to branch '%s'" % debian_branch)

    # Merge with starting branch
    repo.git.merge(branch)
    print_green("Merged branch '%s' into '%s'" % (brnorm, debian_branch))

    # Compute python and debian version
    cd(repo_dir)
    python_version = versioning.get_python_version()
    debian_version = versioning.\
        debian_version_from_python_version(python_version)
    print_green("The new debian version will be: '%s'" % debian_version)

    # Update changelog
    dch = git_dch("--debian-branch=%s" % debian_branch,
                  "--git-author",
                  "--ignore-regex=\".*\"",
                  "--multimaint-merge",
                  "--since=HEAD",
                  "--new-version=%s" % debian_version)
    print_green("Successfully ran '%s'" % " ".join(dch.cmd))

    if mode == "release":
        # Commit changelog and update tag branches
        call("vim debian/changelog")
        repo.git.add("debian/changelog")
        repo.git.commit("-s", "-a", m="Bump new upstream version")
        python_tag = python_version
        debian_tag = "debian/" + python_tag
        repo.git.tag(debian_tag)
        repo.git.tag(python_tag, brnorm)
    else:
        f = open("debian/changelog", 'r+')
        lines = f.readlines()
        lines[0] = lines[0].replace("UNRELEASED", "unstable")
        lines[2] = lines[2].replace("UNRELEASED", "Snapshot version")
        f.seek(0)
        f.writelines(lines)
        f.close()
        repo.git.add("debian/changelog")

    # Update the python version files
    # TODO: remove this
    for package in packages:
        # python setup.py should run in its directory
        cd(package)
        package_dir = repo_dir + "/" + package
        res = python(package_dir + "/setup.py", "sdist", _out=sys.stdout)
        print res.stdout
        if package != ".":
            cd("../")

    # Add version.py files to repo
    call("grep \"__version_vcs\" -r . -l -I | xargs git add -f")

    # Create debian branches
    build_dir = options.build_dir
    if not options.build_dir:
        build_dir = create_temp_directory("df-build")
        print_green("Created directory '%s' to store the .deb files." %
                    build_dir)

    cd(repo_dir)
    call("git-buildpackage --git-export-dir=%s --git-upstream-branch=%s"
         " --git-debian-branch=%s --git-export=INDEX --git-ignore-new -sa"
         % (build_dir, brnorm, debian_branch))

    # Remove cloned repo
    if mode != 'release' and not options.keep_repo:
        print_green("Removing cloned repo '%s'." % repo_dir)
        rm("-r", repo_dir)
    else:
        print_green("Repository dir '%s'" % repo_dir)

    print_green("Completed. Version '%s', build area: '%s'"
                % (debian_version, build_dir))

    # Print help message
    if mode == "release":
        TAG_MSG = "Tagged branch %s with tag %s\n"
        print_green(TAG_MSG % (brnorm, python_tag))
        print_green(TAG_MSG % (debian_branch, debian_tag))

        UPDATE_MSG = "To update repository %s, go to %s, and run the"\
                     " following commands:\n" + "git push origin %s\n" * 3

        origin_url = repo.remotes['origin'].url
        remote_url = original_repo.remotes['origin'].url

        print_green(UPDATE_MSG % (origin_url, repo_dir, debian_branch,
                    debian_tag, python_tag))
        print_green(UPDATE_MSG % (remote_url, original_repo.working_dir,
                    debian_branch, debian_tag, python_tag))


def get_packages_to_build(config_file):
    config_file = os.path.abspath(config_file)
    try:
        f = open(config_file)
    except IOError as e:
        raise IOError("Can not access configuration file %s: %s"
                      % (config_file, e.strerror))

    lines = [l.strip() for l in f.readlines()]
    l = [l for l in lines if not l.startswith("#")]
    f.close()
    return l


def create_temp_directory(suffix):
    create_dir_cmd = mktemp("-d", "/tmp/" + suffix + "-XXXXX")
    return create_dir_cmd.stdout.strip()


def call(cmd):
    rc = os.system(cmd)
    if rc:
        raise RuntimeError("Command '%s' failed!" % cmd)

if __name__ == "__main__":
    sys.exit(main())
