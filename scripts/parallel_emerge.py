#!/usr/bin/python2.6
# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Program to run emerge in parallel, for significant speedup.

Usage:
 ./parallel_emerge [--board=BOARD] [--workon=PKGS]
                   [--force-remote-binary=PKGS] [emerge args] package

Basic operation:
  Runs 'emerge -p --debug' to display dependencies, and stores a
  dependency graph. All non-blocked packages are launched in parallel,
  as 'emerge --nodeps package' with any blocked packages being emerged
  immediately upon deps being met.

  For this to work effectively, /usr/lib/portage/pym/portage/locks.py
  must be stubbed out, preventing portage from slowing itself with
  unneccesary locking, as this script ensures that emerge is run in such
  a way that common resources are never in conflict. This is controlled
  by an environment variable PORTAGE_LOCKS set in parallel emerge
  subprocesses.

  Parallel Emerge unlocks two things during operation, here's what you
  must do to keep this safe:
    * Storage dir containing binary packages. - Don't emerge new
      packages while installing the existing ones.
    * Portage database - You must not examine deps while modifying the
      database. Therefore you may only parallelize "-p" read only access,
      or "--nodeps" write only access.
  Caveats:
    * Some ebuild packages have incorrectly specified deps, and running
      them in parallel is more likely to bring out these failures.
    * Some ebuilds (especially the build part) have complex dependencies
      that are not captured well by this script (it may be necessary to
      install an old package to build, but then install a newer version
      of the same package for a runtime dep).
"""

import codecs
import copy
import errno
import heapq
import multiprocessing
import os
import Queue
import signal
import sys
import tempfile
import time
import traceback

# If PORTAGE_USERNAME isn't specified, scrape it from the $HOME variable. On
# Chromium OS, the default "portage" user doesn't have the necessary
# permissions. It'd be easier if we could default to $USERNAME, but $USERNAME
# is "root" here because we get called through sudo.
#
# We need to set this before importing any portage modules, because portage
# looks up "PORTAGE_USERNAME" at import time.
#
# NOTE: .bashrc sets PORTAGE_USERNAME = $USERNAME, so most people won't
# encounter this case unless they have an old chroot or blow away the
# environment by running sudo without the -E specifier.
if "PORTAGE_USERNAME" not in os.environ:
  homedir = os.environ.get("HOME")
  if homedir:
    os.environ["PORTAGE_USERNAME"] = os.path.basename(homedir)

# Portage doesn't expose dependency trees in its public API, so we have to
# make use of some private APIs here. These modules are found under
# /usr/lib/portage/pym/.
#
# TODO(davidjames): Update Portage to expose public APIs for these features.
from _emerge.actions import adjust_configs
from _emerge.actions import load_emerge_config
from _emerge.create_depgraph_params import create_depgraph_params
from _emerge.depgraph import backtrack_depgraph
from _emerge.main import emerge_main
from _emerge.main import parse_opts
from _emerge.Package import Package
from _emerge.Scheduler import Scheduler
from _emerge.SetArg import SetArg
from _emerge.stdout_spinner import stdout_spinner
from portage._global_updates import _global_updates
from portage.versions import vercmp
import portage
import portage.debug

def Usage():
  """Print usage."""
  print "Usage:"
  print " ./parallel_emerge [--board=BOARD] [--workon=PKGS]"
  print "                   [--rebuild] [emerge args] package"
  print
  print "Packages specified as workon packages are always built from source."
  print
  print "The --workon argument is mainly useful when you want to build and"
  print "install packages that you are working on unconditionally, but do not"
  print "to have to rev the package to indicate you want to build it from"
  print "source. The build_packages script will automatically supply the"
  print "workon argument to emerge, ensuring that packages selected using"
  print "cros-workon are rebuilt."
  print
  print "The --rebuild option rebuilds packages whenever their dependencies"
  print "are changed. This ensures that your build is correct."
  sys.exit(1)


# Global start time
GLOBAL_START = time.time()

# Whether process has been killed by a signal.
KILLED = multiprocessing.Event()


class EmergeData(object):
  """This simple struct holds various emerge variables.

  This struct helps us easily pass emerge variables around as a unit.
  These variables are used for calculating dependencies and installing
  packages.
  """

  __slots__ = ["action", "cmdline_packages", "depgraph", "favorites",
               "mtimedb", "opts", "root_config", "scheduler_graph",
               "settings", "spinner", "trees"]

  def __init__(self):
    # The action the user requested. If the user is installing packages, this
    # is None. If the user is doing anything other than installing packages,
    # this will contain the action name, which will map exactly to the
    # long-form name of the associated emerge option.
    #
    # Example: If you call parallel_emerge --unmerge package, the action name
    #          will be "unmerge"
    self.action = None

    # The list of packages the user passed on the command-line.
    self.cmdline_packages = None

    # The emerge dependency graph. It'll contain all the packages involved in
    # this merge, along with their versions.
    self.depgraph = None

    # The list of candidates to add to the world file.
    self.favorites = None

    # A dict of the options passed to emerge. This dict has been cleaned up
    # a bit by parse_opts, so that it's a bit easier for the emerge code to
    # look at the options.
    #
    # Emerge takes a few shortcuts in its cleanup process to make parsing of
    # the options dict easier. For example, if you pass in "--usepkg=n", the
    # "--usepkg" flag is just left out of the dictionary altogether. Because
    # --usepkg=n is the default, this makes parsing easier, because emerge
    # can just assume that if "--usepkg" is in the dictionary, it's enabled.
    #
    # These cleanup processes aren't applied to all options. For example, the
    # --with-bdeps flag is passed in as-is.  For a full list of the cleanups
    # applied by emerge, see the parse_opts function in the _emerge.main
    # package.
    self.opts = None

    # A dictionary used by portage to maintain global state. This state is
    # loaded from disk when portage starts up, and saved to disk whenever we
    # call mtimedb.commit().
    #
    # This database contains information about global updates (i.e., what
    # version of portage we have) and what we're currently doing. Portage
    # saves what it is currently doing in this database so that it can be
    # resumed when you call it with the --resume option.
    #
    # parallel_emerge does not save what it is currently doing in the mtimedb,
    # so we do not support the --resume option.
    self.mtimedb = None

    # The portage configuration for our current root. This contains the portage
    # settings (see below) and the three portage trees for our current root.
    # (The three portage trees are explained below, in the documentation for
    #  the "trees" member.)
    self.root_config = None

    # The scheduler graph is used by emerge to calculate what packages to
    # install. We don't actually install any deps, so this isn't really used,
    # but we pass it in to the Scheduler object anyway.
    self.scheduler_graph = None

    # Portage settings for our current session. Most of these settings are set
    # in make.conf inside our current install root.
    self.settings = None

    # The spinner, which spews stuff to stdout to indicate that portage is
    # doing something. We maintain our own spinner, so we set the portage
    # spinner to "silent" mode.
    self.spinner = None

    # The portage trees. There are separate portage trees for each root. To get
    # the portage tree for the current root, you can look in self.trees[root],
    # where root = self.settings["ROOT"].
    #
    # In each root, there are three trees: vartree, porttree, and bintree.
    #  - vartree: A database of the currently-installed packages.
    #  - porttree: A database of ebuilds, that can be used to build packages.
    #  - bintree: A database of binary packages.
    self.trees = None


class DepGraphGenerator(object):
  """Grab dependency information about packages from portage.

  Typical usage:
    deps = DepGraphGenerator()
    deps.Initialize(sys.argv[1:])
    deps_tree, deps_info = deps.GenDependencyTree()
    deps_graph = deps.GenDependencyGraph(deps_tree, deps_info)
    deps.PrintTree(deps_tree)
    PrintDepsMap(deps_graph)
  """

  __slots__ = ["board", "emerge", "package_db", "show_output"]

  def __init__(self):
    self.board = None
    self.emerge = EmergeData()
    self.package_db = {}
    self.show_output = False

  def ParseParallelEmergeArgs(self, argv):
    """Read the parallel emerge arguments from the command-line.

    We need to be compatible with emerge arg format.  We scrape arguments that
    are specific to parallel_emerge, and pass through the rest directly to
    emerge.
    Args:
      argv: arguments list
    Returns:
      Arguments that don't belong to parallel_emerge
    """
    emerge_args = []
    for arg in argv:
      # Specifically match arguments that are specific to parallel_emerge, and
      # pass through the rest.
      if arg.startswith("--board="):
        self.board = arg.replace("--board=", "")
      elif arg.startswith("--workon="):
        workon_str = arg.replace("--workon=", "")
        emerge_args.append("--reinstall-atoms=%s" % workon_str)
        emerge_args.append("--usepkg-exclude=%s" % workon_str)
      elif arg.startswith("--force-remote-binary="):
        force_remote_binary = arg.replace("--force-remote-binary=", "")
        emerge_args.append("--useoldpkg-atoms=%s" % force_remote_binary)
      elif arg == "--show-output":
        self.show_output = True
      elif arg == "--rebuild":
        emerge_args.append("--rebuild-if-unbuilt")
      else:
        # Not one of our options, so pass through to emerge.
        emerge_args.append(arg)

    # These packages take a really long time to build, so, for expediency, we
    # are blacklisting them from automatic rebuilds because one of their
    # dependencies needs to be recompiled.
    for pkg in ("chromeos-base/chromeos-chrome", "media-plugins/o3d",
                "dev-java/icedtea"):
      emerge_args.append("--rebuild-exclude=%s" % pkg)

    return emerge_args

  def Initialize(self, args):
    """Initializer. Parses arguments and sets up portage state."""

    # Parse and strip out args that are just intended for parallel_emerge.
    emerge_args = self.ParseParallelEmergeArgs(args)

    # Setup various environment variables based on our current board. These
    # variables are normally setup inside emerge-${BOARD}, but since we don't
    # call that script, we have to set it up here. These variables serve to
    # point our tools at /build/BOARD and to setup cross compiles to the
    # appropriate board as configured in toolchain.conf.
    if self.board:
      os.environ["PORTAGE_CONFIGROOT"] = "/build/" + self.board
      os.environ["PORTAGE_SYSROOT"] = "/build/" + self.board
      os.environ["SYSROOT"] = "/build/" + self.board

      # Although CHROMEOS_ROOT isn't specific to boards, it's normally setup
      # inside emerge-${BOARD}, so we set it up here for compatibility. It
      # will be going away soon as we migrate to CROS_WORKON_SRCROOT.
      os.environ.setdefault("CHROMEOS_ROOT", os.environ["HOME"] + "/trunk")

    # Turn off interactive delays
    os.environ["EBEEP_IGNORE"] = "1"
    os.environ["EPAUSE_IGNORE"] = "1"
    os.environ["UNMERGE_DELAY"] = "0"

    # Parse the emerge options.
    action, opts, cmdline_packages = parse_opts(emerge_args, silent=True)

    # Set environment variables based on options. Portage normally sets these
    # environment variables in emerge_main, but we can't use that function,
    # because it also does a bunch of other stuff that we don't want.
    # TODO(davidjames): Patch portage to move this logic into a function we can
    # reuse here.
    if "--debug" in opts:
      os.environ["PORTAGE_DEBUG"] = "1"
    if "--config-root" in opts:
      os.environ["PORTAGE_CONFIGROOT"] = opts["--config-root"]
    if "--root" in opts:
      os.environ["ROOT"] = opts["--root"]
    if "--accept-properties" in opts:
      os.environ["ACCEPT_PROPERTIES"] = opts["--accept-properties"]

    # Portage has two flags for doing collision protection: collision-protect
    # and protect-owned. The protect-owned feature is enabled by default and
    # is quite useful: it checks to make sure that we don't have multiple
    # packages that own the same file. The collision-protect feature is more
    # strict, and less useful: it fails if it finds a conflicting file, even
    # if that file was created by an earlier ebuild that failed to install.
    #
    # We want to disable collision-protect here because we don't handle
    # failures during the merge step very well. Sometimes we leave old files
    # lying around and they cause problems, so for now we disable the flag.
    # TODO(davidjames): Look for a better solution.
    features = os.environ.get("FEATURES", "") + " -collision-protect"

    # Install packages in parallel.
    features = features + " parallel-install"

    # If we're installing packages to the board, and we're not using the
    # official flag, we can enable the following optimizations:
    #  1) Don't lock during install step. This allows multiple packages to be
    #     installed at once. This is safe because our board packages do not
    #     muck with each other during the post-install step.
    #  2) Don't update the environment until the end of the build. This is
    #     safe because board packages don't need to run during the build --
    #     they're cross-compiled, so our CPU architecture doesn't support them
    #     anyway.
    if self.board and os.environ.get("CHROMEOS_OFFICIAL") != "1":
      os.environ.setdefault("PORTAGE_LOCKS", "false")
      features = features + " -ebuild-locks no-env-update"

    os.environ["FEATURES"] = features

    # Now that we've setup the necessary environment variables, we can load the
    # emerge config from disk.
    settings, trees, mtimedb = load_emerge_config()

    # Add in EMERGE_DEFAULT_OPTS, if specified.
    tmpcmdline = []
    if "--ignore-default-opts" not in opts:
      tmpcmdline.extend(settings["EMERGE_DEFAULT_OPTS"].split())
    tmpcmdline.extend(emerge_args)
    action, opts, cmdline_packages = parse_opts(tmpcmdline)

    # If we're installing to the board, we want the --root-deps option so that
    # portage will install the build dependencies to that location as well.
    if self.board:
      opts.setdefault("--root-deps", True)

    # Check whether our portage tree is out of date. Typically, this happens
    # when you're setting up a new portage tree, such as in setup_board and
    # make_chroot. In that case, portage applies a bunch of global updates
    # here. Once the updates are finished, we need to commit any changes
    # that the global update made to our mtimedb, and reload the config.
    #
    # Portage normally handles this logic in emerge_main, but again, we can't
    # use that function here.
    if _global_updates(trees, mtimedb["updates"]):
      mtimedb.commit()
      settings, trees, mtimedb = load_emerge_config(trees=trees)

    # Setup implied options. Portage normally handles this logic in
    # emerge_main.
    if "--buildpkgonly" in opts or "buildpkg" in settings.features:
      opts.setdefault("--buildpkg", True)
    if "--getbinpkgonly" in opts:
      opts.setdefault("--usepkgonly", True)
      opts.setdefault("--getbinpkg", True)
    if "getbinpkg" in settings.features:
      # Per emerge_main, FEATURES=getbinpkg overrides --getbinpkg=n
      opts["--getbinpkg"] = True
    if "--getbinpkg" in opts or "--usepkgonly" in opts:
      opts.setdefault("--usepkg", True)
    if "--fetch-all-uri" in opts:
      opts.setdefault("--fetchonly", True)
    if "--skipfirst" in opts:
      opts.setdefault("--resume", True)
    if "--buildpkgonly" in opts:
      # --buildpkgonly will not merge anything, so it overrides all binary
      # package options.
      for opt in ("--getbinpkg", "--getbinpkgonly",
                  "--usepkg", "--usepkgonly"):
        opts.pop(opt, None)
    if (settings.get("PORTAGE_DEBUG", "") == "1" and
        "python-trace" in settings.features):
      portage.debug.set_trace(True)

    # Complain about unsupported options
    for opt in ("--ask", "--ask-enter-invalid", "--resume", "--skipfirst"):
      if opt in opts:
        print "%s is not supported by parallel_emerge" % opt
        sys.exit(1)

    # Make emerge specific adjustments to the config (e.g. colors!)
    adjust_configs(opts, trees)

    # Save our configuration so far in the emerge object
    emerge = self.emerge
    emerge.action, emerge.opts = action, opts
    emerge.settings, emerge.trees, emerge.mtimedb = settings, trees, mtimedb
    emerge.cmdline_packages = cmdline_packages
    root = settings["ROOT"]
    emerge.root_config = trees[root]["root_config"]

    if "--usepkg" in opts:
      emerge.trees[root]["bintree"].populate("--getbinpkg" in opts)

  def CreateDepgraph(self, emerge, packages):
    """Create an emerge depgraph object."""
    # Setup emerge options.
    emerge_opts = emerge.opts.copy()

    # Ask portage to build a dependency graph. with the options we specified
    # above.
    params = create_depgraph_params(emerge_opts, emerge.action)
    success, depgraph, favorites = backtrack_depgraph(
        emerge.settings, emerge.trees, emerge_opts, params, emerge.action,
        packages, emerge.spinner)
    emerge.depgraph = depgraph

    # Is it impossible to honor the user's request? Bail!
    if not success:
      depgraph.display_problems()
      sys.exit(1)

    emerge.depgraph = depgraph
    emerge.favorites = favorites

    # Prime and flush emerge caches.
    root = emerge.settings["ROOT"]
    vardb = emerge.trees[root]["vartree"].dbapi
    if "--pretend" not in emerge.opts:
      vardb.counter_tick()
    vardb.flush_cache()

  def GenDependencyTree(self):
    """Get dependency tree info from emerge.

    Returns:
      Dependency tree
    """
    start = time.time()

    emerge = self.emerge

    # Create a list of packages to merge
    packages = set(emerge.cmdline_packages[:])

    # Tell emerge to be quiet. We print plenty of info ourselves so we don't
    # need any extra output from portage.
    portage.util.noiselimit = -1

    # My favorite feature: The silent spinner. It doesn't spin. Ever.
    # I'd disable the colors by default too, but they look kind of cool.
    emerge.spinner = stdout_spinner()
    emerge.spinner.update = emerge.spinner.update_quiet

    if "--quiet" not in emerge.opts:
      print "Calculating deps..."

    self.CreateDepgraph(emerge, packages)
    depgraph = emerge.depgraph

    # Build our own tree from the emerge digraph.
    deps_tree = {}
    digraph = depgraph._dynamic_config.digraph
    root = emerge.settings["ROOT"]
    final_db = depgraph._dynamic_config.mydbapi[root]
    for node, node_deps in digraph.nodes.items():
      # Calculate dependency packages that need to be installed first. Each
      # child on the digraph is a dependency. The "operation" field specifies
      # what we're doing (e.g. merge, uninstall, etc.). The "priorities" array
      # contains the type of dependency (e.g. build, runtime, runtime_post,
      # etc.)
      #
      # Portage refers to the identifiers for packages as a CPV. This acronym
      # stands for Component/Path/Version.
      #
      # Here's an example CPV: chromeos-base/power_manager-0.0.1-r1
      # Split up, this CPV would be:
      #   C -- Component: chromeos-base
      #   P -- Path:      power_manager
      #   V -- Version:   0.0.1-r1
      #
      # We just refer to CPVs as packages here because it's easier.
      deps = {}
      for child, priorities in node_deps[0].items():
        if isinstance(child, Package) and child.root == root:
          cpv = str(child.cpv)
          action = str(child.operation)

          # If we're uninstalling a package, check whether Portage is
          # installing a replacement. If so, just depend on the installation
          # of the new package, because the old package will automatically
          # be uninstalled at that time.
          if action == "uninstall":
            for pkg in final_db.match_pkgs(child.slot_atom):
              cpv = str(pkg.cpv)
              action = "merge"
              break

          deps[cpv] = dict(action=action,
                           deptypes=[str(x) for x in priorities],
                           deps={})

      # We've built our list of deps, so we can add our package to the tree.
      if isinstance(node, Package) and node.root == root:
        deps_tree[str(node.cpv)] = dict(action=str(node.operation),
                                        deps=deps)

    # Ask portage for its install plan, so that we can only throw out
    # dependencies that portage throws out.
    deps_info = {}
    for pkg in depgraph.altlist():
      if isinstance(pkg, Package):
        assert pkg.root == root
        self.package_db[pkg.cpv] = pkg

        # Save off info about the package
        deps_info[str(pkg.cpv)] = {"idx": len(deps_info)}

    seconds = time.time() - start
    if "--quiet" not in emerge.opts:
      print "Deps calculated in %dm%.1fs" % (seconds / 60, seconds % 60)

    return deps_tree, deps_info

  def PrintTree(self, deps, depth=""):
    """Print the deps we have seen in the emerge output.

    Args:
     deps: Dependency tree structure.
     depth: Allows printing the tree recursively, with indentation.
    """
    for entry in sorted(deps):
      action = deps[entry]["action"]
      print "%s %s (%s)" % (depth, entry, action)
      self.PrintTree(deps[entry]["deps"], depth=depth + "  ")

  def GenDependencyGraph(self, deps_tree, deps_info):
    """Generate a doubly linked dependency graph.

    Args:
      deps_tree: Dependency tree structure.
      deps_info: More details on the dependencies.
    Returns:
      Deps graph in the form of a dict of packages, with each package
      specifying a "needs" list and "provides" list.
    """
    emerge = self.emerge
    root = emerge.settings["ROOT"]

    # deps_map is the actual dependency graph.
    #
    # Each package specifies a "needs" list and a "provides" list. The "needs"
    # list indicates which packages we depend on. The "provides" list
    # indicates the reverse dependencies -- what packages need us.
    #
    # We also provide some other information in the dependency graph:
    #  - action: What we're planning on doing with this package. Generally,
    #            "merge", "nomerge", or "uninstall"
    deps_map = {}

    def ReverseTree(packages):
      """Convert tree to digraph.

      Take the tree of package -> requirements and reverse it to a digraph of
      buildable packages -> packages they unblock.
      Args:
        packages: Tree(s) of dependencies.
      Returns:
        Unsanitized digraph.
      """
      binpkg_phases = set(["setup", "preinst", "postinst"])
      needed_dep_types = set(["blocker", "buildtime", "runtime"])
      for pkg in packages:

        # Create an entry for the package
        action = packages[pkg]["action"]
        default_pkg = {"needs": {}, "provides": set(), "action": action,
                       "nodeps": False, "binary": False}
        this_pkg = deps_map.setdefault(pkg, default_pkg)

        if pkg in deps_info:
          this_pkg["idx"] = deps_info[pkg]["idx"]

        # If a package doesn't have any defined phases that might use the
        # dependent packages (i.e. pkg_setup, pkg_preinst, or pkg_postinst),
        # we can install this package before its deps are ready.
        emerge_pkg = self.package_db.get(pkg)
        if emerge_pkg and emerge_pkg.type_name == "binary":
          this_pkg["binary"] = True
          defined_phases = emerge_pkg.metadata.defined_phases
          defined_binpkg_phases = binpkg_phases.intersection(defined_phases)
          if not defined_binpkg_phases:
            this_pkg["nodeps"] = True

        # Create entries for dependencies of this package first.
        ReverseTree(packages[pkg]["deps"])

        # Add dependencies to this package.
        for dep, dep_item in packages[pkg]["deps"].iteritems():
          # We only need to enforce strict ordering of dependencies if the
          # dependency is a blocker, or is a buildtime or runtime dependency.
          # (I.e., ignored, optional, and runtime_post dependencies don't
          # depend on ordering.)
          dep_types = dep_item["deptypes"]
          if needed_dep_types.intersection(dep_types):
            deps_map[dep]["provides"].add(pkg)
            this_pkg["needs"][dep] = "/".join(dep_types)

          # If there's a blocker, Portage may need to move files from one
          # package to another, which requires editing the CONTENTS files of
          # both packages. To avoid race conditions while editing this file,
          # the two packages must not be installed in parallel, so we can't
          # safely ignore dependencies. See http://crosbug.com/19328
          if "blocker" in dep_types:
            this_pkg["nodeps"] = False

    def FindCycles():
      """Find cycles in the dependency tree.

      Returns:
        A dict mapping cyclic packages to a dict of the deps that cause
        cycles. For each dep that causes cycles, it returns an example
        traversal of the graph that shows the cycle.
      """

      def FindCyclesAtNode(pkg, cycles, unresolved, resolved):
        """Find cycles in cyclic dependencies starting at specified package.

        Args:
          pkg: Package identifier.
          cycles: A dict mapping cyclic packages to a dict of the deps that
                  cause cycles. For each dep that causes cycles, it returns an
                  example traversal of the graph that shows the cycle.
          unresolved: Nodes that have been visited but are not fully processed.
          resolved: Nodes that have been visited and are fully processed.
        """
        pkg_cycles = cycles.get(pkg)
        if pkg in resolved and not pkg_cycles:
          # If we already looked at this package, and found no cyclic
          # dependencies, we can stop now.
          return
        unresolved.append(pkg)
        for dep in deps_map[pkg]["needs"]:
          if dep in unresolved:
            idx = unresolved.index(dep)
            mycycle = unresolved[idx:] + [dep]
            for i in range(len(mycycle) - 1):
              pkg1, pkg2 = mycycle[i], mycycle[i+1]
              cycles.setdefault(pkg1, {}).setdefault(pkg2, mycycle)
          elif not pkg_cycles or dep not in pkg_cycles:
            # Looks like we haven't seen this edge before.
            FindCyclesAtNode(dep, cycles, unresolved, resolved)
        unresolved.pop()
        resolved.add(pkg)

      cycles, unresolved, resolved = {}, [], set()
      for pkg in deps_map:
        FindCyclesAtNode(pkg, cycles, unresolved, resolved)
      return cycles

    def RemoveUnusedPackages():
      """Remove installed packages, propagating dependencies."""
      # Schedule packages that aren't on the install list for removal
      rm_pkgs = set(deps_map.keys()) - set(deps_info.keys())

      # Remove the packages we don't want, simplifying the graph and making
      # it easier for us to crack cycles.
      for pkg in sorted(rm_pkgs):
        this_pkg = deps_map[pkg]
        needs = this_pkg["needs"]
        provides = this_pkg["provides"]
        for dep in needs:
          dep_provides = deps_map[dep]["provides"]
          dep_provides.update(provides)
          dep_provides.discard(pkg)
          dep_provides.discard(dep)
        for target in provides:
          target_needs = deps_map[target]["needs"]
          target_needs.update(needs)
          target_needs.pop(pkg, None)
          target_needs.pop(target, None)
        del deps_map[pkg]

    def PrintCycleBreak(basedep, dep, mycycle):
      """Print details about a cycle that we are planning on breaking.

         We are breaking a cycle where dep needs basedep. mycycle is an
         example cycle which contains dep -> basedep."""

      needs = deps_map[dep]["needs"]
      depinfo = needs.get(basedep, "deleted")

      # It's OK to swap install order for blockers, as long as the two
      # packages aren't installed in parallel. If there is a cycle, then
      # we know the packages depend on each other already, so we can drop the
      # blocker safely without printing a warning.
      if depinfo == "blocker":
        return

      # Notify the user that we're breaking a cycle.
      print "Breaking %s -> %s (%s)" % (dep, basedep, depinfo)

      # Show cycle.
      for i in range(len(mycycle) - 1):
        pkg1, pkg2 = mycycle[i], mycycle[i+1]
        needs = deps_map[pkg1]["needs"]
        depinfo = needs.get(pkg2, "deleted")
        if pkg1 == dep and pkg2 == basedep:
          depinfo = depinfo + ", deleting"
        print "  %s -> %s (%s)" % (pkg1, pkg2, depinfo)

    def SanitizeTree():
      """Remove circular dependencies.

      We prune all dependencies involved in cycles that go against the emerge
      ordering. This has a nice property: we're guaranteed to merge
      dependencies in the same order that portage does.

      Because we don't treat any dependencies as "soft" unless they're killed
      by a cycle, we pay attention to a larger number of dependencies when
      merging. This hurts performance a bit, but helps reliability.
      """
      start = time.time()
      cycles = FindCycles()
      while cycles:
        for dep, mycycles in cycles.iteritems():
          for basedep, mycycle in mycycles.iteritems():
            if deps_info[basedep]["idx"] >= deps_info[dep]["idx"]:
              if "--quiet" not in emerge.opts:
                PrintCycleBreak(basedep, dep, mycycle)
              del deps_map[dep]["needs"][basedep]
              deps_map[basedep]["provides"].remove(dep)
        cycles = FindCycles()
      seconds = time.time() - start
      if "--quiet" not in emerge.opts and seconds >= 0.1:
        print "Tree sanitized in %dm%.1fs" % (seconds / 60, seconds % 60)

    def FindRecursiveProvides(pkg, seen):
      """Find all nodes that require a particular package.

      Assumes that graph is acyclic.

      Args:
        pkg: Package identifier.
        seen: Nodes that have been visited so far.
      """
      if pkg in seen:
        return
      seen.add(pkg)
      info = deps_map[pkg]
      info["tprovides"] = info["provides"].copy()
      for dep in info["provides"]:
        FindRecursiveProvides(dep, seen)
        info["tprovides"].update(deps_map[dep]["tprovides"])

    ReverseTree(deps_tree)

    # We need to remove unused packages so that we can use the dependency
    # ordering of the install process to show us what cycles to crack.
    RemoveUnusedPackages()
    SanitizeTree()
    seen = set()
    for pkg in deps_map:
      FindRecursiveProvides(pkg, seen)
    return deps_map

  def PrintInstallPlan(self, deps_map):
    """Print an emerge-style install plan.

    The install plan lists what packages we're installing, in order.
    It's useful for understanding what parallel_emerge is doing.

    Args:
      deps_map: The dependency graph.
    """

    def InstallPlanAtNode(target, deps_map):
      nodes = []
      nodes.append(target)
      for dep in deps_map[target]["provides"]:
        del deps_map[dep]["needs"][target]
        if not deps_map[dep]["needs"]:
          nodes.extend(InstallPlanAtNode(dep, deps_map))
      return nodes

    deps_map = copy.deepcopy(deps_map)
    install_plan = []
    plan = set()
    for target, info in deps_map.iteritems():
      if not info["needs"] and target not in plan:
        for item in InstallPlanAtNode(target, deps_map):
          plan.add(item)
          install_plan.append(self.package_db[item])

    for pkg in plan:
      del deps_map[pkg]

    if deps_map:
      print "Cyclic dependencies:", " ".join(deps_map)
      PrintDepsMap(deps_map)
      sys.exit(1)

    self.emerge.depgraph.display(install_plan)


def PrintDepsMap(deps_map):
  """Print dependency graph, for each package list it's prerequisites."""
  for i in sorted(deps_map):
    print "%s: (%s) needs" % (i, deps_map[i]["action"])
    needs = deps_map[i]["needs"]
    for j in sorted(needs):
      print "    %s" % (j)
    if not needs:
      print "    no dependencies"


class EmergeJobState(object):
  __slots__ = ["done", "filename", "last_notify_timestamp", "last_output_seek",
               "last_output_timestamp", "pkgname", "retcode", "start_timestamp",
               "target"]

  def __init__(self, target, pkgname, done, filename, start_timestamp,
               retcode=None):

    # The full name of the target we're building (e.g.
    # chromeos-base/chromeos-0.0.1-r60)
    self.target = target

    # The short name of the target we're building (e.g. chromeos-0.0.1-r60)
    self.pkgname = pkgname

    # Whether the job is done. (True if the job is done; false otherwise.)
    self.done = done

    # The filename where output is currently stored.
    self.filename = filename

    # The timestamp of the last time we printed the name of the log file. We
    # print this at the beginning of the job, so this starts at
    # start_timestamp.
    self.last_notify_timestamp = start_timestamp

    # The location (in bytes) of the end of the last complete line we printed.
    # This starts off at zero. We use this to jump to the right place when we
    # print output from the same ebuild multiple times.
    self.last_output_seek = 0

    # The timestamp of the last time we printed output. Since we haven't
    # printed output yet, this starts at zero.
    self.last_output_timestamp = 0

    # The return code of our job, if the job is actually finished.
    self.retcode = retcode

    # The timestamp when our job started.
    self.start_timestamp = start_timestamp


def KillHandler(signum, frame):
  # Kill self and all subprocesses.
  os.killpg(0, signal.SIGKILL)

def SetupWorkerSignals():
  def ExitHandler(signum, frame):
    # Set KILLED flag.
    KILLED.set()

    # Remove our signal handlers so we don't get called recursively.
    signal.signal(signal.SIGINT, KillHandler)
    signal.signal(signal.SIGTERM, KillHandler)

  # Ensure that we exit quietly and cleanly, if possible, when we receive
  # SIGTERM or SIGINT signals. By default, when the user hits CTRL-C, all
  # of the child processes will print details about KeyboardInterrupt
  # exceptions, which isn't very helpful.
  signal.signal(signal.SIGINT, ExitHandler)
  signal.signal(signal.SIGTERM, ExitHandler)

def EmergeProcess(scheduler, output):
  """Merge a package in a subprocess.

  Args:
    scheduler: Scheduler object.
    output: Temporary file to write output.

  Returns:
    The exit code returned by the subprocess.
  """
  pid = os.fork()
  if pid == 0:
    try:
      # Sanity checks.
      if sys.stdout.fileno() != 1: raise Exception("sys.stdout.fileno() != 1")
      if sys.stderr.fileno() != 2: raise Exception("sys.stderr.fileno() != 2")

      # - Redirect 1 (stdout) and 2 (stderr) at our temporary file.
      # - Redirect 0 to point at sys.stdin. In this case, sys.stdin
      #   points at a file reading os.devnull, because multiprocessing mucks
      #   with sys.stdin.
      # - Leave the sys.stdin and output filehandles alone.
      fd_pipes = {0: sys.stdin.fileno(),
                  1: output.fileno(),
                  2: output.fileno(),
                  sys.stdin.fileno(): sys.stdin.fileno(),
                  output.fileno(): output.fileno()}
      portage.process._setup_pipes(fd_pipes)

      # Portage doesn't like when sys.stdin.fileno() != 0, so point sys.stdin
      # at the filehandle we just created in _setup_pipes.
      if sys.stdin.fileno() != 0:
        sys.stdin = os.fdopen(0, "r")

      # Actually do the merge.
      retval = scheduler.merge()

    # We catch all exceptions here (including SystemExit, KeyboardInterrupt,
    # etc) so as to ensure that we don't confuse the multiprocessing module,
    # which expects that all forked children exit with os._exit().
    except:
      traceback.print_exc(file=output)
      retval = 1
    sys.stdout.flush()
    sys.stderr.flush()
    output.flush()
    os._exit(retval)
  else:
    # Return the exit code of the subprocess.
    return os.waitpid(pid, 0)[1]

def EmergeWorker(task_queue, job_queue, emerge, package_db):
  """This worker emerges any packages given to it on the task_queue.

  Args:
    task_queue: The queue of tasks for this worker to do.
    job_queue: The queue of results from the worker.
    emerge: An EmergeData() object.
    package_db: A dict, mapping package ids to portage Package objects.

  It expects package identifiers to be passed to it via task_queue. When
  a task is started, it pushes the (target, filename) to the started_queue.
  The output is stored in filename. When a merge starts or finishes, we push
  EmergeJobState objects to the job_queue.
  """

  SetupWorkerSignals()
  settings, trees, mtimedb = emerge.settings, emerge.trees, emerge.mtimedb

  # Disable flushing of caches to save on I/O.
  root = emerge.settings["ROOT"]
  vardb = emerge.trees[root]["vartree"].dbapi
  vardb._flush_cache_enabled = False

  opts, spinner = emerge.opts, emerge.spinner
  opts["--nodeps"] = True
  while True:
    # Wait for a new item to show up on the queue. This is a blocking wait,
    # so if there's nothing to do, we just sit here.
    target = task_queue.get()
    if not target:
      # If target is None, this means that the main thread wants us to quit.
      # The other workers need to exit too, so we'll push the message back on
      # to the queue so they'll get it too.
      task_queue.put(target)
      return
    if KILLED.is_set():
      return

    db_pkg = package_db[target]
    db_pkg.root_config = emerge.root_config
    install_list = [db_pkg]
    pkgname = db_pkg.pf
    output = tempfile.NamedTemporaryFile(prefix=pkgname + "-", delete=False)
    start_timestamp = time.time()
    job = EmergeJobState(target, pkgname, False, output.name, start_timestamp)
    job_queue.put(job)
    if "--pretend" in opts:
      retcode = 0
    else:
      try:
        emerge.scheduler_graph.mergelist = install_list
        scheduler = Scheduler(settings, trees, mtimedb, opts, spinner,
            favorites=emerge.favorites, graph_config=emerge.scheduler_graph)

        # Enable blocker handling even though we're in --nodeps mode. This
        # allows us to unmerge the blocker after we've merged the replacement.
        scheduler._opts_ignore_blockers = frozenset()

        retcode = EmergeProcess(scheduler, output)
      except Exception:
        traceback.print_exc(file=output)
        retcode = 1
      output.close()

    if KILLED.is_set():
      return

    job = EmergeJobState(target, pkgname, True, output.name, start_timestamp,
                         retcode)
    job_queue.put(job)


class LinePrinter(object):
  """Helper object to print a single line."""

  def __init__(self, line):
    self.line = line

  def Print(self, seek_locations):
    print self.line


class JobPrinter(object):
  """Helper object to print output of a job."""

  def __init__(self, job, unlink=False):
    """Print output of job.

    If unlink is True, unlink the job output file when done."""
    self.current_time = time.time()
    self.job = job
    self.unlink = unlink

  def Print(self, seek_locations):

    job = self.job

    # Calculate how long the job has been running.
    seconds = self.current_time - job.start_timestamp

    # Note that we've printed out the job so far.
    job.last_output_timestamp = self.current_time

    # Note that we're starting the job
    info = "job %s (%dm%.1fs)" % (job.pkgname, seconds / 60, seconds % 60)
    last_output_seek = seek_locations.get(job.filename, 0)
    if last_output_seek:
      print "=== Continue output for %s ===" % info
    else:
      print "=== Start output for %s ===" % info

    # Print actual output from job
    f = codecs.open(job.filename, encoding='utf-8', errors='replace')
    f.seek(last_output_seek)
    prefix = job.pkgname + ":"
    for line in f:

      # Save off our position in the file
      if line and line[-1] == "\n":
        last_output_seek = f.tell()
        line = line[:-1]

      # Print our line
      print prefix, line.encode('utf-8', 'replace')
    f.close()

    # Save our last spot in the file so that we don't print out the same
    # location twice.
    seek_locations[job.filename] = last_output_seek

    # Note end of output section
    if job.done:
      print "=== Complete: %s ===" % info
    else:
      print "=== Still running: %s ===" % info

    if self.unlink:
      os.unlink(job.filename)


def PrintWorker(queue):
  """A worker that prints stuff to the screen as requested."""

  def ExitHandler(signum, frame):
    # Set KILLED flag.
    KILLED.set()

    # Switch to default signal handlers so that we'll die after two signals.
    signal.signal(signal.SIGINT, KillHandler)
    signal.signal(signal.SIGTERM, KillHandler)

  # Don't exit on the first SIGINT / SIGTERM, because the parent worker will
  # handle it and tell us when we need to exit.
  signal.signal(signal.SIGINT, ExitHandler)
  signal.signal(signal.SIGTERM, ExitHandler)

  # seek_locations is a map indicating the position we are at in each file.
  # It starts off empty, but is set by the various Print jobs as we go along
  # to indicate where we left off in each file.
  seek_locations = {}
  while True:
    try:
      job = queue.get()
      if job:
        job.Print(seek_locations)
        sys.stdout.flush()
      else:
        break
    except IOError as ex:
      if ex.errno == errno.EINTR:
        # Looks like we received a signal. Keep printing.
        continue
      raise

class EmergeQueue(object):
  """Class to schedule emerge jobs according to a dependency graph."""

  def __init__(self, deps_map, emerge, package_db, show_output):
    # Store the dependency graph.
    self._deps_map = deps_map
    # Initialize the running queue to empty
    self._jobs = {}
    self._ready = []
    # List of total package installs represented in deps_map.
    install_jobs = [x for x in deps_map if deps_map[x]["action"] == "merge"]
    self._total_jobs = len(install_jobs)
    self._show_output = show_output

    if "--pretend" in emerge.opts:
      print "Skipping merge because of --pretend mode."
      sys.exit(0)

    # Set a process group so we can easily terminate all children.
    os.setsid()

    # Setup scheduler graph object. This is used by the child processes
    # to help schedule jobs.
    emerge.scheduler_graph = emerge.depgraph.schedulerGraph()

    # Calculate how many jobs we can run in parallel. We don't want to pass
    # the --jobs flag over to emerge itself, because that'll tell emerge to
    # hide its output, and said output is quite useful for debugging hung
    # jobs.
    procs = min(self._total_jobs,
                emerge.opts.pop("--jobs", multiprocessing.cpu_count()))
    self._load_avg = emerge.opts.pop("--load-average", None)
    self._emerge_queue = multiprocessing.Queue()
    self._job_queue = multiprocessing.Queue()
    self._print_queue = multiprocessing.Queue()
    args = (self._emerge_queue, self._job_queue, emerge, package_db)
    self._pool = multiprocessing.Pool(procs, EmergeWorker, args)
    self._print_worker = multiprocessing.Process(target=PrintWorker,
                                                 args=[self._print_queue])
    self._print_worker.start()

    # Initialize the failed queue to empty.
    self._retry_queue = []
    self._failed = set()

    # Setup an exit handler so that we print nice messages if we are
    # terminated.
    self._SetupExitHandler()

    # Schedule our jobs.
    for target, info in deps_map.items():
      if info["nodeps"] or not info["needs"]:
        score = (-len(info["tprovides"]), info["binary"], info["idx"])
        self._ready.append((score, target))
    heapq.heapify(self._ready)
    self._procs = procs
    self._ScheduleLoop()

    # Print an update.
    self._Status()

  def _SetupExitHandler(self):

    def ExitHandler(signum, frame):
      # Set KILLED flag.
      KILLED.set()

      # Kill our signal handlers so we don't get called recursively
      signal.signal(signal.SIGINT, KillHandler)
      signal.signal(signal.SIGTERM, KillHandler)

      # Print our current job status
      for target, job in self._jobs.iteritems():
        if job:
          self._print_queue.put(JobPrinter(job, unlink=True))

      # Notify the user that we are exiting
      self._Print("Exiting on signal %s" % signum)
      self._print_queue.put(None)
      self._print_worker.join()

      # Kill child threads, then exit.
      os.killpg(0, signal.SIGKILL)
      sys.exit(1)

    # Print out job status when we are killed
    signal.signal(signal.SIGINT, ExitHandler)
    signal.signal(signal.SIGTERM, ExitHandler)

  def _Schedule(self, target):
    # We maintain a tree of all deps, if this doesn't need
    # to be installed just free up its children and continue.
    # It is possible to reinstall deps of deps, without reinstalling
    # first level deps, like so:
    # chromeos (merge) -> eselect (nomerge) -> python (merge)
    this_pkg = self._deps_map.get(target)
    if this_pkg is None:
      pass
    elif this_pkg["action"] == "nomerge":
      self._Finish(target)
    elif target not in self._jobs:
      # Kick off the build if it's marked to be built.
      self._jobs[target] = None
      self._emerge_queue.put(target)
      return True

  def _ScheduleLoop(self):
    # If the current load exceeds our desired load average, don't schedule
    # more than one job.
    if self._load_avg and os.getloadavg()[0] > self._load_avg:
      needed_jobs = 1
    else:
      needed_jobs = self._procs

    # Schedule more jobs.
    while self._ready and len(self._jobs) < needed_jobs:
      score, pkg = heapq.heappop(self._ready)
      if pkg not in self._failed:
        self._Schedule(pkg)

  def _Print(self, line):
    """Print a single line."""
    self._print_queue.put(LinePrinter(line))

  def _Status(self):
    """Print status."""
    current_time = time.time()
    no_output = True

    # Print interim output every minute if --show-output is used. Otherwise,
    # print notifications about running packages every 2 minutes, and print
    # full output for jobs that have been running for 60 minutes or more.
    if self._show_output:
      interval = 60
      notify_interval = 0
    else:
      interval = 60 * 60
      notify_interval = 60 * 2
    for target, job in self._jobs.iteritems():
      if job:
        last_timestamp = max(job.start_timestamp, job.last_output_timestamp)
        if last_timestamp + interval < current_time:
          self._print_queue.put(JobPrinter(job))
          job.last_output_timestamp = current_time
          no_output = False
        elif (notify_interval and
              job.last_notify_timestamp + notify_interval < current_time):
          job_seconds = current_time - job.start_timestamp
          args = (job.pkgname, job_seconds / 60, job_seconds % 60, job.filename)
          info = "Still building %s (%dm%.1fs). Logs in %s" % args
          job.last_notify_timestamp = current_time
          self._Print(info)
          no_output = False

    # If we haven't printed any messages yet, print a general status message
    # here.
    if no_output:
      seconds = current_time - GLOBAL_START
      line = ("Pending %s, Ready %s, Running %s, Retrying %s, Total %s "
              "[Time %dm%.1fs Load %s]")
      load =  " ".join(str(x) for x in os.getloadavg())
      self._Print(line % (len(self._deps_map), len(self._ready),
                          len(self._jobs), len(self._retry_queue),
                          self._total_jobs, seconds / 60, seconds % 60, load))

  def _Finish(self, target):
    """Mark a target as completed and unblock dependencies."""
    this_pkg = self._deps_map[target]
    if this_pkg["needs"] and this_pkg["nodeps"]:
      # We got installed, but our deps have not been installed yet. Dependent
      # packages should only be installed when our needs have been fully met.
      this_pkg["action"] = "nomerge"
    else:
      finish = []
      for dep in this_pkg["provides"]:
        dep_pkg = self._deps_map[dep]
        del dep_pkg["needs"][target]
        if not dep_pkg["needs"]:
          if dep_pkg["nodeps"] and dep_pkg["action"] == "nomerge":
            self._Finish(dep)
          else:
            score = (-len(dep_pkg["tprovides"]), dep_pkg["binary"],
                     dep_pkg["idx"])
            heapq.heappush(self._ready, (score, dep))
      self._deps_map.pop(target)

  def _Retry(self):
    while self._retry_queue:
      target = self._retry_queue.pop(0)
      if self._Schedule(target):
        self._Print("Retrying emerge of %s." % target)
        break

  def _Exit(self):
    # Tell emerge workers to exit. They all exit when 'None' is pushed
    # to the queue.
    self._emerge_queue.put(None)
    self._pool.close()
    self._pool.join()
    self._emerge_queue.close()
    self._emerge_queue = None

    # Now that our workers are finished, we can kill the print queue.
    self._print_queue.put(None)
    self._print_worker.join()
    self._print_queue.close()
    self._print_queue = None
    self._job_queue.close()
    self._job_queue = None

  def Run(self):
    """Run through the scheduled ebuilds.

    Keep running so long as we have uninstalled packages in the
    dependency graph to merge.
    """
    retried = set()
    while self._deps_map:
      # Check here that we are actually waiting for something.
      if (self._emerge_queue.empty() and
          self._job_queue.empty() and
          not self._jobs and
          not self._ready and
          self._deps_map):
        # If we have failed on a package, retry it now.
        if self._retry_queue:
          self._Retry()
        else:
          # Tell child threads to exit.
          self._Exit()

          # The dependency map is helpful for debugging failures.
          PrintDepsMap(self._deps_map)

          # Tell the user why we're exiting.
          if self._failed:
            print "Packages failed: %s" % " ,".join(self._failed)
          else:
            print "Deadlock! Circular dependencies!"
          sys.exit(1)

      for i in range(3):
        try:
          job = self._job_queue.get(timeout=5)
          break
        except Queue.Empty:
          # Check if any more jobs can be scheduled.
          self._ScheduleLoop()
      else:
        # Print an update every 15 seconds.
        self._Status()
        continue

      target = job.target

      if not job.done:
        self._jobs[target] = job
        self._Print("Started %s (logged in %s)" % (target, job.filename))
        continue

      # Print output of job
      if self._show_output or job.retcode != 0:
        self._print_queue.put(JobPrinter(job, unlink=True))
      else:
        os.unlink(job.filename)
      del self._jobs[target]

      seconds = time.time() - job.start_timestamp
      details = "%s (in %dm%.1fs)" % (target, seconds / 60, seconds % 60)
      previously_failed = target in self._failed

      # Complain if necessary.
      if job.retcode != 0:
        # Handle job failure.
        if previously_failed:
          # If this job has failed previously, give up.
          self._Print("Failed %s. Your build has failed." % details)
        else:
          # Queue up this build to try again after a long while.
          retried.add(target)
          self._retry_queue.append(target)
          self._failed.add(target)
          self._Print("Failed %s, retrying later." % details)
      else:
        if previously_failed:
          # Remove target from list of failed packages.
          self._failed.remove(target)

        self._Print("Completed %s" % details)

        # Mark as completed and unblock waiting ebuilds.
        self._Finish(target)

        if previously_failed and self._retry_queue:
          # If we have successfully retried a failed package, and there
          # are more failed packages, try the next one. We will only have
          # one retrying package actively running at a time.
          self._Retry()


      # Schedule pending jobs and print an update.
      self._ScheduleLoop()
      self._Status()

    # If packages were retried, output a warning.
    if retried:
      self._Print("")
      self._Print("WARNING: The following packages failed the first time,")
      self._Print("but succeeded upon retry. This might indicate incorrect")
      self._Print("dependencies.")
      for pkg in retried:
        self._Print("  %s" % pkg)
      self._Print("@@@STEP_WARNINGS@@@")
      self._Print("")

    # Tell child threads to exit.
    self._Print("Merge complete")
    self._Exit()


def main(argv):

  parallel_emerge_args = argv[:]
  deps = DepGraphGenerator()
  deps.Initialize(parallel_emerge_args)
  emerge = deps.emerge

  if emerge.action is not None:
    argv = deps.ParseParallelEmergeArgs(argv)
    sys.exit(emerge_main(argv))
  elif not emerge.cmdline_packages:
    Usage()
    sys.exit(1)

  # Unless we're in pretend mode, there's not much point running without
  # root access. We need to be able to install packages.
  #
  # NOTE: Even if you're running --pretend, it's a good idea to run
  #       parallel_emerge with root access so that portage can write to the
  #       dependency cache. This is important for performance.
  if "--pretend" not in emerge.opts and portage.secpass < 2:
    print "parallel_emerge: superuser access is required."
    sys.exit(1)

  if "--quiet" not in emerge.opts:
    cmdline_packages = " ".join(emerge.cmdline_packages)
    print "Starting fast-emerge."
    print " Building package %s on %s" % (cmdline_packages,
                                          deps.board or "root")

  deps_tree, deps_info = deps.GenDependencyTree()

  # You want me to be verbose? I'll give you two trees! Twice as much value.
  if "--tree" in emerge.opts and "--verbose" in emerge.opts:
    deps.PrintTree(deps_tree)

  deps_graph = deps.GenDependencyGraph(deps_tree, deps_info)

  # OK, time to print out our progress so far.
  deps.PrintInstallPlan(deps_graph)
  if "--tree" in emerge.opts:
    PrintDepsMap(deps_graph)

  # Are we upgrading portage? If so, and there are more packages to merge,
  # schedule a restart of parallel_emerge to merge the rest. This ensures that
  # we pick up all updates to portage settings before merging any more
  # packages.
  portage_upgrade = False
  root = emerge.settings["ROOT"]
  final_db = emerge.depgraph._dynamic_config.mydbapi[root]
  if root == "/":
    for db_pkg in final_db.match_pkgs("sys-apps/portage"):
      portage_pkg = deps_graph.get(db_pkg.cpv)
      if portage_pkg and len(deps_graph) > 1:
        portage_pkg["needs"].clear()
        portage_pkg["provides"].clear()
        deps_graph = { str(db_pkg.cpv): portage_pkg }
        portage_upgrade = True
        if "--quiet" not in emerge.opts:
          print "Upgrading portage first, then restarting..."

  # Run the queued emerges.
  scheduler = EmergeQueue(deps_graph, emerge, deps.package_db, deps.show_output)
  scheduler.Run()
  scheduler = None

  # Update environment (library cache, symlinks, etc.)
  if deps.board and "--pretend" not in emerge.opts:
    # Turn off env-update suppression used above for disabling
    # env-update during merging.
    os.environ["FEATURES"] += " -no-env-update"
    # Also kick the existing settings should they be reused...
    if hasattr(portage, 'settings'):
      portage.settings.unlock()
      portage.settings.features.discard('no-env-update')
    portage.env_update()

  # If we already upgraded portage, we don't need to do so again. But we do
  # need to upgrade the rest of the packages. So we'll go ahead and do that.
  #
  # In order to grant the child permission to run setsid, we need to run sudo
  # again. We preserve SUDO_USER here in case an ebuild depends on it.
  if portage_upgrade:
    args = ["sudo", "-E", "SUDO_USER=%s" % os.environ.get("SUDO_USER", "")]
    args += ['parallel_emerge'] + parallel_emerge_args
    args += ["--exclude=sys-apps/portage"]
    os.execvp("sudo", args)

  print "Done"
  sys.exit(0)