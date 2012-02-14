#!/usr/bin/python

# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unittests for build stages."""

import mox
import os
import shutil
import StringIO
import sys
import tempfile
import unittest

import constants
sys.path.insert(0, constants.SOURCE_ROOT)
from chromite.buildbot import builderstage as bs
from chromite.buildbot import cbuildbot
from chromite.buildbot import cbuildbot_background as background
from chromite.buildbot import cbuildbot_config as config
from chromite.buildbot import cbuildbot_commands as commands
from chromite.buildbot import cbuildbot_results as results_lib
from chromite.buildbot import cbuildbot_stages as stages
from chromite.buildbot import lkgm_manager
from chromite.buildbot import manifest_version
from chromite.buildbot import repository
from chromite.buildbot import portage_utilities
from chromite.lib import cros_build_lib as cros_lib
from chromite.lib import cros_test_lib


# pylint: disable=E1120,W0212,R0904
class AbstractStageTest(mox.MoxTestBase):
  """Base class for tests that test a particular build stage.

  Abstract base class that sets up the build config and options with some
  default values for testing BuilderStage and its derivatives.
  """

  TRACKING_BRANCH = 'ooga_booga'

  def ConstructStage(self):
    """Returns an instance of the stage to be tested.
    Implement in subclasses.
    """
    raise NotImplementedError, "return an instance of stage to be tested."

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    # Always stub RunCommmand out as we use it in every method.
    self.bot_id = 'x86-generic-pre-flight-queue'
    self.build_config = config.config[self.bot_id].copy()
    self.build_root = '/fake_root'
    self._boards = self.build_config['boards']
    self._current_board = self._boards[0]

    self.url = 'fake_url'
    self.build_config['git_url'] = self.url

    # Use the cbuildbot parser to create properties and populate default values.
    parser = cbuildbot._CreateParser()
    (self.options, _) = parser.parse_args([])

    self.options.buildroot = self.build_root
    self.options.buildbot = True
    self.options.debug = False
    self.options.prebuilts = False
    self.options.clobber = False
    self.options.buildnumber = 1234
    self.overlay = os.path.join(self.build_root,
                                'src/third_party/chromiumos-overlay')

    bs.BuilderStage.SetTrackingBranch(self.TRACKING_BRANCH)
    portage_utilities._OVERLAY_LIST_CMD = '/bin/true'

  def RunStage(self):
    """Creates and runs an instance of the stage to be tested.
    Requires ConstructStage() to be implemented.

    Raises:
      NotImplementedError: ConstructStage() was not implemented.
    """

    # Stage construction is usually done as late as possible because the tests
    # set up the build configuration and options used in constructing the stage.

    stage = self.ConstructStage()
    stage.Run()


class BuilderStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

  def ConstructStage(self):
    return bs.BuilderStage(self.bot_id, self.options, self.build_config)

  def testGetPortageEnvVar(self):
    """Basic test case for _GetPortageEnvVar function."""
    self.mox.StubOutWithMock(cros_lib, 'RunCommand')
    envvar = 'EXAMPLE'
    obj = cros_test_lib.EasyAttr(output='RESULT\n')
    cros_lib.RunCommand(mox.And(mox.IsA(list), mox.In(envvar)),
                        cwd='%s/src/scripts' % self.build_root,
                        redirect_stdout=True, enter_chroot=True,
                        error_ok=True).AndReturn(obj)
    self.mox.ReplayAll()
    stage = self.ConstructStage()
    board = self._current_board
    result = stage._GetPortageEnvVar(envvar, board)
    self.mox.VerifyAll()
    self.assertEqual(result, 'RESULT')


class ManifestVersionedSyncStageTest(AbstractStageTest):
  """Tests the two (heavily related) stages ManifestVersionedSync, and
     ManifestVersionedSyncCompleted.
  """
  # pylint: disable=W0223

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

    self.tmpdir = tempfile.mkdtemp()
    self.source_repo = 'ssh://source/repo'
    self.manifest_version_url = 'fake manifest url'
    self.branch = 'master'
    self.build_name = 'x86-generic'
    self.incr_type = 'branch'

    self.build_config['manifest_version'] = self.manifest_version_url
    self.next_version = 'next_version'

    repo = repository.RepoRepository(
      self.source_repo, self.tmpdir, self.branch)
    self.manager = manifest_version.BuildSpecsManager(
      repo, self.manifest_version_url, self.build_name,
      self.incr_type, dry_run=True)

    stages.ManifestVersionedSyncStage.manifest_manager = self.manager

  def tearDown(self):
    if os.path.exists(self.tmpdir): shutil.rmtree(self.tmpdir)

  def testManifestVersionedSyncOnePartBranch(self):
    """Tests basic ManifestVersionedSyncStage with branch ooga_booga"""
    self.mox.StubOutWithMock(stages.ManifestVersionedSyncStage,
                             'Initialize')
    self.mox.StubOutWithMock(manifest_version.BuildSpecsManager,
                             'GetNextBuildSpec')
    self.mox.StubOutWithMock(stages.SyncStage, 'ManifestCheckout')

    stages.ManifestVersionedSyncStage.Initialize()
    self.manager.GetNextBuildSpec().AndReturn(self.next_version)

    stages.SyncStage.ManifestCheckout(self.next_version)

    self.mox.ReplayAll()
    stage = stages.ManifestVersionedSyncStage(self.bot_id,
                                              self.options,
                                              self.build_config)
    stage.Run()
    self.mox.VerifyAll()

  def testManifestVersionedSyncCompletedSuccess(self):
    """Tests basic ManifestVersionedSyncStageCompleted on success"""

    stages.ManifestVersionedSyncStage.manifest_manager = self.manager

    self.mox.StubOutWithMock(manifest_version.BuildSpecsManager, 'UpdateStatus')

    self.manager.UpdateStatus(success=True)

    self.mox.ReplayAll()
    stage = stages.ManifestVersionedSyncCompletionStage(self.bot_id,
                                                        self.options,
                                                        self.build_config,
                                                        success=True)
    stage.Run()
    self.mox.VerifyAll()

  def testManifestVersionedSyncCompletedFailure(self):
    """Tests basic ManifestVersionedSyncStageCompleted on failure"""

    stages.ManifestVersionedSyncStage.manifest_manager = self.manager

    self.mox.StubOutWithMock(manifest_version.BuildSpecsManager, 'UpdateStatus')

    self.manager.UpdateStatus(success=False)


    self.mox.ReplayAll()
    stage = stages.ManifestVersionedSyncCompletionStage(self.bot_id,
                                                        self.options,
                                                        self.build_config,
                                                        success=False)
    stage.Run()
    self.mox.VerifyAll()

  def testManifestVersionedSyncCompletedIncomplete(self):
    """Tests basic ManifestVersionedSyncStageCompleted on incomplete build."""

    stages.ManifestVersionedSyncStage.manifest_manager = None

    self.mox.ReplayAll()
    stage = stages.ManifestVersionedSyncCompletionStage(self.bot_id,
                                                        self.options,
                                                        self.build_config,
                                                        success=False)
    stage.Run()
    self.mox.VerifyAll()


class LKGMCandidateSyncCompletionStage(AbstractStageTest):
  """Tests the two (heavily related) stages ManifestVersionedSync, and
     ManifestVersionedSyncCompleted.
  """

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

    self.tmpdir = tempfile.mkdtemp()
    self.source_repo = 'ssh://source/repo'
    self.manifest_version_url = 'fake manifest url'
    self.branch = 'master'
    self.build_name = 'x86-generic-pre-flight-queue'
    self.build_type = constants.PFQ_TYPE

    self.build_config['manifest_version'] = True
    self.build_config['build_type'] = self.build_type

    repo = repository.RepoRepository(
      self.source_repo, self.tmpdir, self.branch)
    self.manager = lkgm_manager.LKGMManager(
      repo, self.manifest_version_url, self.build_name,
      self.build_type, incr_type='branch', dry_run=True)

  def ConstructStage(self):
    return stages.LKGMCandidateSyncCompletionStage(self.bot_id, self.options,
                                                   self.build_config,
                                                   success=True)

  def testGetImportantBuildsForMaster(self):
    test_config = {}
    test_config['test1'] = {
        'manifest_version': True,
        'build_type': constants.PFQ_TYPE,
        'overlays': 'public',
        'important': True,
        'chrome_rev': None,
    }
    test_config['test2'] = {
        'manifest_version': True,
        'build_type': constants.PFQ_TYPE,
        'overlays': 'public',
        'important': True,
        'chrome_rev': None,
    }
    test_config['test3'] = {
        'manifest_version': True,
        'build_type': constants.PFQ_TYPE,
        'overlays': 'public',
        'important': False,
        'chrome_rev': None,
    }
    test_config['test4'] = {
        'manifest_version': False,
        'build_type': constants.PFQ_TYPE,
        'overlays': 'public',
        'important': True,
        'chrome_rev': None,
    }

    self.mox.ReplayAll()
    stage = self.ConstructStage()
    important_configs = stage._GetImportantBuildersForMaster(test_config)
    self.mox.VerifyAll()

    self.assertTrue('test1' in important_configs)
    self.assertTrue('test2' in important_configs)
    self.assertFalse('test3' in important_configs)
    self.assertFalse('test4' in important_configs)


class BuildBoardTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.mox.StubOutWithMock(os.path, 'isdir')

  def ConstructStage(self):
    return stages.BuildBoardStage(self.bot_id, self.options, self.build_config)

  def testFullBuild(self):
    """Tests whether we correctly run make chroot and setup board for a full."""
    self.bot_id = 'x86-generic-full'
    self.build_config = config.config[self.bot_id]
    self.mox.StubOutWithMock(commands, 'MakeChroot')
    self.mox.StubOutWithMock(commands, 'SetupBoard')

    os.path.isdir(os.path.join(self.build_root, 'chroot')).AndReturn(True)
    commands.MakeChroot(buildroot=self.build_root,
                        replace=True,
                        use_sdk=True,
                        chrome_root=None,
                        extra_env=mox.IgnoreArg()
                        )
    os.path.isdir(os.path.join(self.build_root, 'chroot', 'build',
                               self._current_board)).AndReturn(False)
    commands.SetupBoard(self.build_root,
                        board=self._current_board,
                        fast=True,
                        usepkg=False,
                        latest_toolchain=False,
                        extra_env={},
                        profile=None)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testFullBuildWithProfile(self):
    """Tests whether full builds add profile flag when requested."""
    self.bot_id = 'x86-generic-full'
    self.build_config = config.config[self.bot_id]
    self.mox.StubOutWithMock(commands, 'MakeChroot')
    self.mox.StubOutWithMock(commands, 'SetupBoard')
    self.mox.StubOutWithMock(commands, 'RunChrootUpgradeHooks')

    os.path.isdir(os.path.join(self.build_root, 'chroot')).AndReturn(True)
    commands.MakeChroot(buildroot=self.build_root,
                        replace=True,
                        use_sdk=True,
                        chrome_root=None,
                        extra_env=mox.IgnoreArg()
                        )
    os.path.isdir(os.path.join(self.build_root, 'chroot', 'build',
                               self._current_board)).AndReturn(False)
    commands.SetupBoard(self.build_root,
                        board=self._current_board,
                        fast=True,
                        usepkg=False,
                        latest_toolchain=False,
                        extra_env={},
                        profile=self.build_config['profile'])

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testFullBuildWithOverriddenProfile(self):
    """Tests whether full builds add overridden profile flag when requested."""
    self.bot_id = 'x86-generic-full'
    self.options.profile = 'smock'
    self.build_config = config.config[self.bot_id]
    self.mox.StubOutWithMock(commands, 'MakeChroot')
    self.mox.StubOutWithMock(commands, 'SetupBoard')
    self.mox.StubOutWithMock(commands, 'RunChrootUpgradeHooks')

    os.path.isdir(os.path.join(self.build_root, 'chroot')).AndReturn(True)
    commands.MakeChroot(buildroot=self.build_root,
                        replace=True,
                        use_sdk=True,
                        chrome_root=None,
                        extra_env=mox.IgnoreArg()
                        )
    os.path.isdir(os.path.join(self.build_root, 'chroot', 'build',
                               self._current_board)).AndReturn(False)
    commands.SetupBoard(self.build_root,
                        board=self._current_board,
                        fast=True,
                        usepkg=False,
                        latest_toolchain=False,
                        extra_env={},
                        profile='smock')

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()
    self.options.profile = None

  def testBinBuild(self):
    """Tests whether we skip un-necessary steps for a binary builder."""
    self.mox.StubOutWithMock(commands, 'MakeChroot')
    self.mox.StubOutWithMock(commands, 'SetupBoard')
    self.mox.StubOutWithMock(commands, 'RunChrootUpgradeHooks')

    os.path.isdir(os.path.join(self.build_root, 'chroot')).AndReturn(True)
    os.path.isdir(os.path.join(self.build_root, 'chroot', 'build',
                               self._current_board)).AndReturn(True)

    commands.RunChrootUpgradeHooks(self.build_root)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testBinBuildAfterClobber(self):
    """Tests whether we make chroot and board after a clobber."""
    self.mox.StubOutWithMock(commands, 'MakeChroot')
    self.mox.StubOutWithMock(commands, 'SetupBoard')

    os.path.isdir(os.path.join(self.build_root, 'chroot')).AndReturn(False)
    commands.MakeChroot(buildroot=self.build_root,
                        replace=self.build_config['chroot_replace'],
                        use_sdk=self.build_config['use_sdk'],
                        chrome_root=None,
                        extra_env=mox.IgnoreArg()
                        )

    os.path.isdir(os.path.join(self.build_root, 'chroot', 'build',
                               self._current_board)).AndReturn(False)

    commands.SetupBoard(self.build_root,
                        board=self._current_board,
                        fast=self.build_config['fast'],
                        usepkg=self.build_config['usepkg_setup_board'],
                        latest_toolchain=self.build_config['latest_toolchain'],
                        extra_env={},
                        profile=None)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class VMTestStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.fake_results_dir = '/tmp/fake_results_dir'
    self.fake_chroot_results_dir = '/my/fake_chroot/tmp/fake_results_dir'
    self.mox.StubOutWithMock(background, 'SetNiceness')
    background.SetNiceness(foreground=True)
    self.mox.StubOutWithMock(commands, 'ArchiveTestResults')
    self.archive_stage_mock = self.mox.CreateMock(stages.ArchiveStage)

  def ConstructStage(self):
    return stages.VMTestStage(self.bot_id, self.options, self.build_config,
                              self._current_board, self.archive_stage_mock)

  def testFullTests(self):
    """Tests if full unit and cros_au_test_harness tests are run correctly."""
    self.bot_id = 'x86-generic-full'
    self.build_config = config.config[self.bot_id].copy()
    self.build_config['vm_tests'] = constants.FULL_AU_TEST_TYPE

    self.mox.StubOutWithMock(commands, 'RunTestSuite')
    self.mox.StubOutWithMock(commands, 'CreateTestRoot')
    self.mox.StubOutWithMock(tempfile, 'mkdtemp')

    tempfile.mkdtemp(prefix='cbuildbot').AndReturn(self.fake_results_dir)
    commands.CreateTestRoot(self.build_root).AndReturn(self.fake_results_dir)
    commands.RunTestSuite(self.build_root,
                          self._current_board,
                          mox.IgnoreArg(),
                          os.path.join(self.fake_results_dir,
                                       'test_harness'),
                          build_config=self.bot_id,
                          nplus1_archive_dir=self.fake_results_dir,
                          whitelist_chrome_crashes=True,
                          test_type=constants.FULL_AU_TEST_TYPE)
    commands.ArchiveTestResults(self.build_root, self.fake_results_dir,
                                prefix='').AndReturn('some tarball')
    self.archive_stage_mock.UpdatePayloadsReady(self.fake_results_dir)
    self.archive_stage_mock.TestResultsReady('some tarball')
    self.archive_stage_mock.VMTestStatus(True)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testQuickTests(self):
    """Tests if quick unit and cros_au_test_harness tests are run correctly."""
    self.bot_id = 'x86-generic-full'
    self.build_config = config.config[self.bot_id].copy()
    self.build_config['vm_tests'] = constants.SIMPLE_AU_TEST_TYPE

    self.mox.StubOutWithMock(commands, 'RunTestSuite')
    self.mox.StubOutWithMock(commands, 'CreateTestRoot')
    self.mox.StubOutWithMock(tempfile, 'mkdtemp')

    tempfile.mkdtemp(prefix='cbuildbot').AndReturn(self.fake_results_dir)
    commands.CreateTestRoot(self.build_root).AndReturn(self.fake_results_dir)
    commands.RunTestSuite(self.build_root,
                          self._current_board,
                          mox.IgnoreArg(),
                          os.path.join(self.fake_results_dir,
                                       'test_harness'),
                          build_config=self.bot_id,
                          nplus1_archive_dir=self.fake_results_dir,
                          whitelist_chrome_crashes=True,
                          test_type=constants.SIMPLE_AU_TEST_TYPE)
    commands.ArchiveTestResults(self.build_root, self.fake_results_dir,
                                prefix='').AndReturn('some tarball')
    self.archive_stage_mock.UpdatePayloadsReady(self.fake_results_dir)
    self.archive_stage_mock.TestResultsReady('some tarball')
    self.archive_stage_mock.VMTestStatus(True)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class UnitTestStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.bot_id = 'x86-generic-full'
    self.build_config = config.config[self.bot_id].copy()
    self.mox.StubOutWithMock(commands, 'RunUnitTests')

  def ConstructStage(self):
    return stages.UnitTestStage(self.bot_id, self.options, self.build_config,
                                self._current_board)

  def testQuickTests(self):
    self.build_config['quick_unit'] = True
    commands.RunUnitTests(self.build_root, self._current_board, full=False,
                          nowithdebug=mox.IgnoreArg())
    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testFullTests(self):
    """Tests if full unit and cros_au_test_harness tests are run correctly."""
    self.build_config['quick_unit'] = False
    commands.RunUnitTests(self.build_root, self._current_board, full=True,
                          nowithdebug=mox.IgnoreArg())
    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class HWTestStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.bot_id = 'x86-mario-release'
    self.build_config = config.config[self.bot_id].copy()
    self.archive_stage_mock = self.mox.CreateMock(stages.ArchiveStage)
    self.platform = 'netbook_MARIO'
    self.suite = 'bvt'

  def ConstructStage(self):
    return stages.HWTestStage(self.bot_id, self.options, self.build_config,
                              self._current_board, self.archive_stage_mock,
                              self.suite, self.platform)

  def testWithSuite(self):
    """Test if run correctly with a test suite."""
    self.archive_stage_mock.GetGSUploadLocation().AndReturn('some_url')
    self.archive_stage_mock.WaitForVMTestStatus().AndReturn(True)
    self.archive_stage_mock.WaitForHWTestUploads().AndReturn(True)

    self.mox.StubOutWithMock(commands, 'RunHWTestSuite')
    commands.RunHWTestSuite('some_url',
                            self.suite,
                            self.platform,
                            False)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class UprevStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

    # Disable most paths by default and selectively enable in tests

    self.options.chrome_rev = None
    self.build_config['uprev'] = False
    self.mox.StubOutWithMock(commands, 'MarkChromeAsStable')
    self.mox.StubOutWithMock(commands, 'UprevPackages')
    self.mox.StubOutWithMock(sys, 'exit')

  def ConstructStage(self):
    return stages.UprevStage(self.bot_id, self.options, self.build_config)

  def testChromeRevSuccess(self):
    """Case where MarkChromeAsStable returns an atom.  We shouldn't exit."""
    self.options.chrome_rev = 'tot'
    chrome_atom = 'chromeos-base/chromeos-chrome-12.0.719.0_alpha-r1'

    commands.MarkChromeAsStable(
        self.build_root,
        self.TRACKING_BRANCH,
        self.options.chrome_rev,
        self._boards,
        chrome_root=None,
        chrome_version=None).AndReturn(chrome_atom)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testChromeRevFoundNothing(self):
    """Verify we exit when MarkChromeAsStable doesn't return an atom."""
    self.options.chrome_rev = 'tot'

    commands.MarkChromeAsStable(
        self.build_root,
        self.TRACKING_BRANCH,
        self.options.chrome_rev,
        self._boards,
        chrome_root=None,
        chrome_version=None)

    sys.exit(0)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testBuildRev(self):
    """Uprevving the build without uprevving chrome."""
    self.build_config['uprev'] = True

    commands.UprevPackages(
        self.build_root,
        self._boards,
        [self.overlay])

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testNoRev(self):
    """No paths are enabled."""
    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testUprevAll(self):
    """Uprev both Chrome and built packages."""
    self.build_config['uprev'] = True
    self.options.chrome_rev = 'tot'

    # Even if MarkChromeAsStable didn't find anything to rev,
    # if we rev the build then we don't exit

    commands.MarkChromeAsStable(
        self.build_root,
        self.TRACKING_BRANCH,
        self.options.chrome_rev,
        self._boards,
        chrome_root=None,
        chrome_version=None).AndReturn(None)

    commands.UprevPackages(
        self.build_root,
        self._boards,
        [self.overlay])

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

def _DoSteps(steps):
  for step in steps:
    step()

class BuildTargetStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.images_root = os.path.join(self.build_root,
                                    'src/build/images/x86-generic')
    latest_image_dir = os.path.join(self.images_root, 'latest')
    self.mox.StubOutWithMock(os, 'readlink')
    self.mox.StubOutWithMock(os, 'symlink')
    os.readlink(latest_image_dir).AndReturn('myimage')
    self.latest_cbuildbot = '%s-cbuildbot' % latest_image_dir
    os.symlink('myimage', self.latest_cbuildbot)

    # Disable most paths by default and selectively enable in tests
    self.build_config['vm_tests'] = None
    self.build_config['build_type'] = constants.PFQ_TYPE
    self.build_config['usepkg_chroot'] = False
    self.build_config['fast'] = False

    self.options.prebuilts = True
    self.options.tests = False

    self.mox.StubOutWithMock(commands, 'Build')
    self.mox.StubOutWithMock(commands, 'UploadPrebuilts')
    self.mox.StubOutWithMock(commands, 'BuildImage')
    self.mox.StubOutWithMock(commands, 'BuildVMImageForTesting')
    self.mox.StubOutWithMock(bs.BuilderStage, '_GetPortageEnvVar')
    self.mox.StubOutWithMock(shutil, 'copyfile')
    self.mox.StubOutWithMock(tempfile, 'mkdtemp')
    self.mox.StubOutWithMock(os.path, 'isdir')

    self.mox.StubOutWithMock(background, 'RunParallelSteps')
    background.RunParallelSteps(mox.IgnoreArg()).WithSideEffects(_DoSteps)

    self.mox.StubOutWithMock(commands, 'BuildAutotestTarball')
    self.mox.StubOutWithMock(os, 'rename')
    self.archive_stage_mock = self.mox.CreateMock(stages.ArchiveStage)

  def ConstructStage(self):
    return stages.BuildTargetStage(
        self.bot_id, self.options, self.build_config,
        self._current_board, self.archive_stage_mock)

  def testAllConditionalPaths(self):
    """Enable all paths to get line coverage."""
    self.build_config['vm_tests'] = constants.SIMPLE_AU_TEST_TYPE
    self.options.tests = True
    self.build_config['build_type'] = constants.BUILD_FROM_SOURCE_TYPE
    self.build_config['build_tests'] = True
    self.build_config['archive_build_debug'] = True
    self.build_config['usepkg_chroot'] = True
    self.build_config['usepkg_setup_board'] = True
    self.build_config['usepkg_build_packages'] = True
    self.build_config['images'] = ['base', 'dev', 'test', 'factory_test',
                                   'factory_install']
    self.build_config['fast'] = True
    self.build_config['useflags'] = ['ALPHA', 'BRAVO', 'CHARLIE']
    self.build_config['skip_toolchain_update'] = False
    self.build_config['nowithdebug'] = False

    proper_env = {'USE' : ' '.join(self.build_config['useflags'])}

    # Convenience variables.
    fake_autotest_dir = '/fake/autotest'
    tarball_name = 'autotest.tar.bz2'
    autotest_tarball_path = os.path.join(fake_autotest_dir, tarball_name)

    commands.Build(self.build_root,
                   self._current_board,
                   build_autotest=True,
                   usepkg=True,
                   fast=True,
                   skip_toolchain_update=False,
                   nowithdebug=False,
                   extra_env=proper_env)

    commands.BuildImage(self.build_root, self._current_board,
                        ['test', 'base', 'dev'], extra_env=proper_env)
    commands.BuildVMImageForTesting(self.build_root, self._current_board,
                                    extra_env=proper_env)
    tempfile.mkdtemp(prefix='autotest').AndReturn(fake_autotest_dir)
    commands.BuildAutotestTarball(self.build_root, self._current_board,
                                  autotest_tarball_path)
    self.archive_stage_mock.AutotestTarballReady(autotest_tarball_path)
    os.path.isdir(self.latest_cbuildbot).AndReturn(True)
    self.archive_stage_mock.SetVersion(self.latest_cbuildbot)
    shutil.copyfile(autotest_tarball_path,
                    os.path.join(self.images_root, 'latest-cbuildbot',
                                 tarball_name))

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testFalseBuildArgs(self):
    """Make sure our logic for Build arguments can toggle to false."""
    self.build_config['useflags'] = None

    commands.Build(self.build_root,
                   self._current_board,
                   build_autotest=mox.IgnoreArg(),
                   fast=mox.IgnoreArg(),
                   usepkg=mox.IgnoreArg(),
                   skip_toolchain_update=mox.IgnoreArg(),
                   nowithdebug=mox.IgnoreArg(),
                   extra_env={})
    self.archive_stage_mock.AutotestTarballReady(None)
    commands.BuildImage(self.build_root, self._current_board, ['test'],
                        extra_env={})
    os.path.isdir(self.latest_cbuildbot).AndReturn(True)
    self.archive_stage_mock.SetVersion(self.latest_cbuildbot)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testFalseTestArg(self):
    """Make sure our logic for build test arg can toggle to false."""
    self.build_config['vm_tests'] = None
    self.options.tests = True
    self.options.hw_tests = True
    self.build_config['build_type'] = constants.BUILD_FROM_SOURCE_TYPE
    self.build_config['usepkg_chroot'] = True
    self.build_config['usepkg_setup_board'] = True
    self.build_config['usepkg_build_packages'] = True
    self.build_config['fast'] = True
    self.build_config['useflags'] = ['ALPHA', 'BRAVO', 'CHARLIE']
    self.build_config['nowithdebug'] = True
    self.build_config['skip_toolchain_update'] = False

    proper_env = {'USE' : ' '.join(self.build_config['useflags'])}

    commands.Build(self.build_root,
                   self._current_board,
                   build_autotest=True,
                   usepkg=True,
                   fast=True,
                   skip_toolchain_update=False,
                   nowithdebug=True,
                   extra_env=proper_env)
    self.archive_stage_mock.AutotestTarballReady(None)
    commands.BuildImage(self.build_root, self._current_board, ['test'],
                        extra_env=proper_env)
    os.path.isdir(self.latest_cbuildbot).AndReturn(True)
    self.archive_stage_mock.SetVersion(self.latest_cbuildbot)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


def _replace_archive_path(functor):
  # If/when mox grows the ability to replace parts of a module selectively,
  # use that and kill this.
  # Till then, we just mutate the module and restore it on the way out.
  def f(self):
    original = stages.DEFAULT_ARCHIVE_PATH
    try:
      stages.DEFAULT_ARCHIVE_PATH = self.tempdir
      return functor(self)
    finally:
      stages.DEFAULT_ARCHIVE_PATH = original
  return cros_test_lib.tempdir_decorator(f)


class ArchiveStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

    self._build_config = self.build_config.copy()
    self._build_config['upload_symbols'] = True
    self._build_config['push_image'] = True

  def ConstructStage(self):
    return stages.ArchiveStage(self.bot_id, self.options, self._build_config,
                               self._current_board)

  @_replace_archive_path
  def testArchive(self):
    """Simple did-it-run test."""
    self.mox.StubOutWithMock(stages.ArchiveStage, 'GetVersion')
    stages.ArchiveStage.GetVersion().MultipleTimes().AndReturn('0.0.0.1')

    # TODO(davidjames): Test the individual archive steps as well.
    self.mox.StubOutWithMock(background, 'RunParallelSteps')
    background.RunParallelSteps(mox.IgnoreArg())

    self.mox.StubOutWithMock(commands, 'PushImages')
    commands.PushImages(self.build_root,
                        board=self._current_board,
                        branch_name='master',
                        archive_url=mox.IgnoreArg(),
                        profile=None)

    self.mox.StubOutWithMock(commands, 'RemoveOldArchives')
    commands.RemoveOldArchives(mox.IgnoreArg(), mox.IgnoreArg())
    self.mox.StubOutWithMock(commands, 'UpdateLatestFile')
    self.mox.StubOutWithMock(commands, 'UploadArchivedFile')
    commands.UpdateLatestFile(mox.IgnoreArg(), mox.IgnoreArg())
    commands.UploadArchivedFile(mox.IgnoreArg(), mox.IgnoreArg(),
                                'LATEST', False)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class UploadPrebuiltsStageTest(AbstractStageTest):
  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)
    self.options.chrome_rev = 'tot'
    self.options.prebuilts = True
    self.mox.StubOutWithMock(stages.UploadPrebuiltsStage, '_GetPortageEnvVar')
    self.mox.StubOutWithMock(commands, 'UploadPrebuilts')

  def RunStage(self):
    """Creates and runs an instance of the stage to be tested.
    Requires ConstructStage() to be implemented.

    Raises:
      NotImplementedError: ConstructStage() was not implemented.
    """

    # Stage construction is usually done as late as possible because the tests
    # set up the build configuration and options used in constructing the stage.

    stage = self.ConstructStage()
    stage._PerformStage()

  def ConstructStage(self):
    return stages.UploadPrebuiltsStage(self.bot_id,
                                       self.options,
                                       self.build_config,
                                       self._current_board)

  def ConstructBinhosts(self):
    for board in (self._current_board, None):
      binhost = 'http://binhost/?board=' + str(board)
      stages.UploadPrebuiltsStage._GetPortageEnvVar(stages._PORTAGE_BINHOST,
          board).AndReturn(binhost)

  def testChromeUpload(self):
    """Test uploading of prebuilts for chrome build."""
    self.build_config['build_type'] = constants.CHROME_PFQ_TYPE

    self.ConstructBinhosts()
    commands.UploadPrebuilts(
        self.build_root, self._current_board,
        self.build_config['overlays'],
        self.build_config['build_type'],
        self.options.chrome_rev,
        self.options.buildnumber,
        self.build_config['binhost_bucket'],
        self.build_config['binhost_key'],
        self.build_config['binhost_base_url'],
        self.build_config['use_binhost_package_file'],
        self.build_config['git_sync'],
        mox.IgnoreArg()).MultipleTimes(mox.IgnoreArg())

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()

  def testPreflightUpload(self):
    """Test uploading of prebuilts for preflight build."""
    self.build_config['build_type'] = constants.PFQ_TYPE

    self.ConstructBinhosts()
    commands.UploadPrebuilts(
        self.build_root, self._current_board,
        self.build_config['overlays'],
        self.build_config['build_type'],
        self.options.chrome_rev,
        self.options.buildnumber,
        self.build_config['binhost_bucket'],
        self.build_config['binhost_key'],
        self.build_config['binhost_base_url'],
        self.build_config['use_binhost_package_file'],
        self.build_config['git_sync'],
        mox.IgnoreArg()).MultipleTimes(mox.IgnoreArg())

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class PublishUprevChangesStageTest(AbstractStageTest):

  def setUp(self):
    mox.MoxTestBase.setUp(self)
    AbstractStageTest.setUp(self)

    # Disable most paths by default and selectively enable in tests

    self.build_config['build_type'] = constants.BUILD_FROM_SOURCE_TYPE
    self.options.chrome_rev = constants.CHROME_REV_TOT
    self.options.prebuilts = True
    self.mox.StubOutWithMock(stages.PublishUprevChangesStage,
                             '_GetPortageEnvVar')
    self.mox.StubOutWithMock(commands, 'UploadPrebuilts')
    self.mox.StubOutWithMock(commands, 'UprevPush')

  def ConstructStage(self):
    return stages.PublishUprevChangesStage(self.bot_id,
                                           self.options,
                                           self.build_config)

  def testPush(self):
    """Test values for PublishUprevChanges."""
    commands.UprevPush(
        self.build_root,
        [self.overlay],
        self.options.debug)

    self.mox.ReplayAll()
    self.RunStage()
    self.mox.VerifyAll()


class BuildStagesResultsTest(unittest.TestCase):

  def setUp(self):
    unittest.TestCase.setUp(self)
    # Always stub RunCommmand out as we use it in every method.
    self.bot_id = 'x86-generic-pre-flight-queue'
    self.build_config = config.config[self.bot_id]
    self.build_root = '/fake_root'
    self.url = 'fake_url'

    # Create a class to hold
    class Options(object):
      pass

    self.options = Options()
    self.options.buildroot = self.build_root
    self.options.debug = False
    self.options.prebuilts = False
    self.options.clobber = False
    self.options.buildnumber = 1234
    self.options.chrome_rev = None

    self.failException = Exception("FailStage needs to fail.")

  def _runStages(self):
    """Run a couple of stages so we can capture the results"""

    # Save off our self where FailStage._PerformStage can find it.
    outer_self = self

    class PassStage(bs.BuilderStage):
      """PassStage always works"""
      pass

    class Pass2Stage(bs.BuilderStage):
      """Pass2Stage always works"""
      pass

    class FailStage(bs.BuilderStage):
      """FailStage always throws an exception"""

      def _PerformStage(self):
        """Throw the exception to make us fail."""
        raise outer_self.failException

    # Run two pass stages, and one fail stage.
    PassStage(self.bot_id, self.options, self.build_config).Run()
    Pass2Stage(self.bot_id, self.options, self.build_config).Run()

    self.assertRaises(
      bs.NonBacktraceBuildException,
      FailStage(self.bot_id, self.options, self.build_config).Run)

  def _verifyRunResults(self, expectedResults):

    actualResults = results_lib.Results.Get()

    # Break out the asserts to be per item to make debugging easier
    self.assertEqual(len(expectedResults), len(actualResults))
    for i in xrange(len(expectedResults)):
      name, result, description, runtime = actualResults[i]

      if result != results_lib.Results.SUCCESS:
        self.assertTrue(isinstance(description, str))

      self.assertTrue(runtime >= 0 and runtime < 2.0)
      self.assertEqual(expectedResults[i], (name, result))

  def _PassString(self):
    return results_lib.Results.SPLIT_TOKEN.join(['Pass', 'None', '0\n'])

  def testRunStages(self):
    """Run some stages and verify the captured results"""

    results_lib.Results.Clear()
    self.assertEqual(results_lib.Results.Get(), [])

    self._runStages()

    # Verify that the results are what we expect.
    expectedResults = [
        ('Pass', results_lib.Results.SUCCESS),
        ('Pass2', results_lib.Results.SUCCESS),
        ('Fail', self.failException)]

    self._verifyRunResults(expectedResults)

  def testSuccessTest(self):
    """Run some stages and verify the captured results"""

    results_lib.Results.Clear()
    results_lib.Results.Record('Pass', results_lib.Results.SUCCESS)

    self.assertTrue(results_lib.Results.BuildSucceededSoFar())

    results_lib.Results.Record('Fail', self.failException, time=1)

    self.assertFalse(results_lib.Results.BuildSucceededSoFar())

    results_lib.Results.Record('Pass2', results_lib.Results.SUCCESS)

    self.assertFalse(results_lib.Results.BuildSucceededSoFar())

  def testStagesReportSuccess(self):
    """Tests Stage reporting."""

    stages.ManifestVersionedSyncStage.manifest_manager = None

    # Store off a known set of results and generate a report
    results_lib.Results.Clear()
    results_lib.Results.Record('Pass', results_lib.Results.SUCCESS, time=1)
    results_lib.Results.Record('Pass2', results_lib.Results.SUCCESS, time=2)
    results_lib.Results.Record('Fail', self.failException, time=3)
    results_lib.Results.Record(
        'FailRunCommand',
        cros_lib.RunCommandError(
            'Command "/bin/false /nosuchdir" failed.\n',
            ['/bin/false', '/nosuchdir'], error_code=2), time=4)

    results = StringIO.StringIO()

    results_lib.Results.Report(results)

    expectedResults = (
        "************************************************************\n"
        "** Stage Results\n"
        "************************************************************\n"
        "** PASS Pass (0:00:01)\n"
        "************************************************************\n"
        "** PASS Pass2 (0:00:02)\n"
        "************************************************************\n"
        "** FAIL Fail (0:00:03) with Exception\n"
        "************************************************************\n"
        "** FAIL FailRunCommand (0:00:04) in /bin/false\n"
        "************************************************************\n")

    expectedLines = expectedResults.split('\n')
    actualLines = results.getvalue().split('\n')

    # Break out the asserts to be per item to make debugging easier
    for i in xrange(len(expectedLines)):
      self.assertEqual(expectedLines[i], actualLines[i])
    self.assertEqual(len(expectedLines), len(actualLines))

  def testStagesReportError(self):
    """Tests Stage reporting with exceptions."""

    stages.ManifestVersionedSyncStage.manifest_manager = None

    # Store off a known set of results and generate a report
    results_lib.Results.Clear()
    results_lib.Results.Record('Pass', results_lib.Results.SUCCESS, time=1)
    results_lib.Results.Record('Pass2', results_lib.Results.SUCCESS, time=2)
    results_lib.Results.Record('Fail', self.failException,
                               'failException Msg\nLine 2', time=3)
    results_lib.Results.Record(
        'FailRunCommand',
        cros_lib.RunCommandError(
            'Command "/bin/false /nosuchdir" failed.\n',
            ['/bin/false', '/nosuchdir'], error_code=2),
        'FailRunCommand msg', time=4)

    results = StringIO.StringIO()

    results_lib.Results.Report(results)

    expectedResults = (
        "************************************************************\n"
        "** Stage Results\n"
        "************************************************************\n"
        "** PASS Pass (0:00:01)\n"
        "************************************************************\n"
        "** PASS Pass2 (0:00:02)\n"
        "************************************************************\n"
        "** FAIL Fail (0:00:03) with Exception\n"
        "************************************************************\n"
        "** FAIL FailRunCommand (0:00:04) in /bin/false\n"
        "************************************************************\n"
        "\n"
        "Build failed with:\n"
        "\n"
        "failException Msg\n"
        "Line 2\n")

    expectedLines = expectedResults.split('\n')
    actualLines = results.getvalue().split('\n')

    # Break out the asserts to be per item to make debugging easier
    for i in xrange(len(expectedLines)):
      self.assertEqual(expectedLines[i], actualLines[i])
    self.assertEqual(len(expectedLines), len(actualLines))

  def testStagesReportReleaseTag(self):
    """Tests Release Tag entry in stages report."""

    current_version = "release_tag_string"
    archive_urls = {'board1': 'result_url1',
                    'board2': 'result_url2'}

    # Store off a known set of results and generate a report
    results_lib.Results.Clear()
    results_lib.Results.Record('Pass', results_lib.Results.SUCCESS, time=1)

    results = StringIO.StringIO()

    results_lib.Results.Report(results, archive_urls, current_version)

    expectedResults = (
        "************************************************************\n"
        "** RELEASE VERSION: release_tag_string\n"
        "************************************************************\n"
        "** Stage Results\n"
        "************************************************************\n"
        "** PASS Pass (0:00:01)\n"
        "************************************************************\n"
        "** BUILD ARTIFACTS FOR THIS BUILD CAN BE FOUND AT:\n"
        "**  board1: result_url1\n"
        "@@@STEP_LINK@Artifacts[board1]@result_url1@@@\n"
        "**  board2: result_url2\n"
        "@@@STEP_LINK@Artifacts[board2]@result_url2@@@\n"
        "************************************************************\n")

    expectedLines = expectedResults.split('\n')
    actualLines = results.getvalue().split('\n')

    # Break out the asserts to be per item to make debugging easier
    for i in xrange(len(expectedLines)):
      self.assertEqual(expectedLines[i], actualLines[i])
    self.assertEqual(len(expectedLines), len(actualLines))

  def testSaveCompletedStages(self):
    """Tests that we can save out completed stages."""

    # Run this again to make sure we have the expected results stored
    results_lib.Results.Clear()
    results_lib.Results.Record('Pass', results_lib.Results.SUCCESS)
    results_lib.Results.Record('Fail', self.failException)
    results_lib.Results.Record('Pass2', results_lib.Results.SUCCESS)

    saveFile = StringIO.StringIO()
    results_lib.Results.SaveCompletedStages(saveFile)
    self.assertEqual(saveFile.getvalue(), self._PassString())

  def testRestoreCompletedStages(self):
    """Tests that we can read in completed stages."""

    results_lib.Results.Clear()
    results_lib.Results.RestoreCompletedStages(
        StringIO.StringIO(self._PassString()))

    previous = results_lib.Results.GetPrevious()
    self.assertEqual(previous.keys(), ['Pass'])

  def testRunAfterRestore(self):
    """Tests that we skip previously completed stages."""

    # Fake results_lib.Results.RestoreCompletedStages
    results_lib.Results.Clear()
    results_lib.Results.RestoreCompletedStages(
        StringIO.StringIO(self._PassString()))

    self._runStages()

    # Verify that the results are what we expect.
    expectedResults = [
        ('Pass', results_lib.Results.SUCCESS),
        ('Pass2', results_lib.Results.SUCCESS),
        ('Fail', self.failException)]

    self._verifyRunResults(expectedResults)


if __name__ == '__main__':
  unittest.main()
