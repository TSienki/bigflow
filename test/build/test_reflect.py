import os
import unittest
import pickle

from test import mixins


class SelfBuildOldProjectTestCase(
    mixins.SubprocessMixin,
    mixins.PrototypedDirMixin,
    unittest.TestCase,
):
    proto_dir = "bf-projects/bf_simple_v10"

    def test_should_build_bdist(self):

        # given
        self.assertFileNotExists("dist/*.whl")
        self.subprocess_run(["python", "project_setup.py", "bdist_wheel"])

        # then
        self.assertFileExists("dist/*.whl")


class SelfBuildProjectTestCase(
    mixins.VenvMixin,
    mixins.SubprocessMixin,
    mixins.PrototypedDirMixin,
    unittest.TestCase,
):
    proto_dir = "build/bf-projects/bf_selfbuild_project"

    def runpy_n_dump(self, func_name: str):
        mod, _ = func_name.rsplit(".", 1)
        pycode = f"import {mod}, pickle, os; pickle.dump({func_name}(), os.fdopen(1, 'wb'))"
        r = self.subprocess_run(["python", "-c", pycode], check=True, capture_output=False)
        return pickle.loads(r.stdout)

    def test_should_build_selfpackage_from_installed_wheel(self):

        # build 'whl' package
        self.assertFileNotExists("dist/*.whl")
        self.subprocess_run(["python", "setup.py", "bdist_wheel"])

        # install .whl package
        whl = self.assertFileExists("dist/*.whl")
        self.venv_pip_install(whl)

        # remove original sdist - drop cwd & create a new one
        self.chdir_new_temp()

        # then - check projectname inferring
        self.assertEqual("bf_selfbuild_project", self.runpy_n_dump('bf_selfbuild_project.buildme.infer_project_name'))
        self.assertEqual("bf_selfbuild_project", self.runpy_n_dump('bf_selfbuild_other_package.buildme.infer_project_name'))
        self.assertEqual("bf_selfbuild_project", self.runpy_n_dump('bf_selfbuild_module.infer_project_name'))

        # then - self-build sdist/wheel/egg pacakges
        sdist_pkg = self.runpy_n_dump('bf_selfbuild_project.buildme.build_sdist')
        self.addCleanup(os.unlink, sdist_pkg)
        self.assertRegex(str(sdist_pkg), r".*\.tar\.gz")
        self.assertFileExists(sdist_pkg)

        wheel_pkg = self.runpy_n_dump('bf_selfbuild_project.buildme.build_wheel')
        self.addCleanup(os.unlink, wheel_pkg)
        self.assertRegex(str(wheel_pkg), r".*\.whl")
        self.assertFileExists(wheel_pkg)

        egg_pkg = self.runpy_n_dump('bf_selfbuild_project.buildme.build_egg')
        self.addCleanup(os.unlink, egg_pkg)
        self.assertRegex(str(egg_pkg), r".*\.egg")
        self.assertFileExists(egg_pkg)

        # then - verify materizlied setuppy
        setuppy = self.runpy_n_dump('bf_selfbuild_project.buildme.materialize_setuppy')
        self.addCleanup(os.unlink, setuppy)
        self.assertFileContentRegex(setuppy, "import.*bigflow")
        self.assertFileContentRegex(setuppy, "bf_simple_v11")
