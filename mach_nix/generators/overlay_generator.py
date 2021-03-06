from collections import OrderedDict
from typing import Dict, List

from mach_nix.data.data_interface import NixpkgsDirectory
from mach_nix.generators import ExpressionGenerator
from mach_nix.resolver import ResolvedPkg


def unindent(text: str, remove: int):
    # removes indentation of text
    # also strips leading newlines
    return ''.join(map(lambda l: l[remove:], text.splitlines(keepends=True)))


class OverlaysGenerator(ExpressionGenerator):

    def __init__(self, py_ver, nixpkgs_commit, nixpkgs_tarball_sha256, nixpkgs: NixpkgsDirectory, pypi_fetcher_commit,
                 pypi_fetcher_tarball_sha256, disable_checks, *args, prefer_nixpkgs=True, **kwargs):
        self.nixpkgs = nixpkgs
        self.disable_checks = disable_checks
        self.prefer_nixpkgs = prefer_nixpkgs
        self.nixpkgs_commit = nixpkgs_commit
        self.nixpkgs_tarball_sha256 = nixpkgs_tarball_sha256
        self.pypi_fetcher_commit = pypi_fetcher_commit
        self.pypi_fetcher_tarball_sha256 = pypi_fetcher_tarball_sha256
        self.py_ver_nix = py_ver.nix()
        super(OverlaysGenerator, self).__init__(*args, **kwargs)

    def generate(self, reqs) -> str:
        pkgs = self.resolver.resolve(
            reqs,
            prefer_nixpkgs=self.prefer_nixpkgs
        )
        pkgs = OrderedDict(sorted(((p.name, p) for p in pkgs), key=lambda x: x[1].name))
        return self._gen_python_env(pkgs)

    def _gen_imports(self):
        out = f"""
            let
              fetchPypiSrc = builtins.fetchTarball {{
                name = "nix-pypi-fetcher";
                url = "https://github.com/DavHau/nix-pypi-fetcher/tarball/{self.pypi_fetcher_commit}";
                # Hash obtained using `nix-prefetch-url --unpack <url>`
                sha256 = "{self.pypi_fetcher_tarball_sha256}";
              }};
              fetchPypi = import (fetchPypiSrc);
              nixpkgs_src = builtins.fetchTarball {{
                name = "nixpkgs";
                url = "https://github.com/nixos/nixpkgs/tarball/{self.nixpkgs_commit}";
                sha256 = "{self.nixpkgs_tarball_sha256}";
              }};
              pkgs = import nixpkgs_src {{ config = {{}}; }};
              python = pkgs.{self.py_ver_nix};
            """
        return unindent(out, 12)

    def _gen_build_inputs(self, build_inputs_local, build_inputs_nixpkgs) -> str:
        build_inputs_str = ' '.join(sorted(build_inputs_local)) + ' ' + ' '.join(
            f"python-self.{b}" for b in sorted(build_inputs_nixpkgs))
        return build_inputs_str

    def _gen_prop_build_inputs(self, prop_build_inputs_local, prop_build_inputs_nixpkgs) -> str:
        prop_build_inputs_str = ' '.join(sorted(prop_build_inputs_local)) + ' ' + ' '.join(
            f"python-self.{b}" for b in sorted(prop_build_inputs_nixpkgs))
        return prop_build_inputs_str

    def _gen_overrideAttrs(self, name, ver, nix_name, build_inputs_str, prop_build_inputs_str):
        out = f"""
            {nix_name} = python-super.{nix_name}.overrideAttrs ( oldAttrs: {{
              name = "{name}-{ver}";
              src = fetchPypi "{name}" "{ver}";"""
        if build_inputs_str:
            out += f"""
              buildInputs = oldAttrs.buildInputs ++ [ {build_inputs_str} ];"""
        if prop_build_inputs_str:
            out += f"""
              propagatedBuildInputs = oldAttrs.propagatedBuildInputs ++ [ {prop_build_inputs_str} ];"""
        if self.disable_checks:
            out += """
              doCheck = false;
              doInstallCheck = false;"""
        out += """
            });\n"""
        return unindent(out, 4)

    def _gen_builPythonPackage(self, name, ver, build_inputs_str, prop_build_inputs_str):
        out = f"""
            {name} = python.pkgs.buildPythonPackage {{
              name = "{name}-{ver}";
              src = fetchPypi "{name}" "{ver}";"""
        if build_inputs_str.strip():
            out += f"""
              buildInputs = [ {build_inputs_str} ];"""
        if prop_build_inputs_str.strip():
            out += f"""
              propagatedBuildInputs = [ {prop_build_inputs_str} ];"""
        if self.disable_checks:
            out += """
              doCheck = false;
              doInstallCheck = false;"""
        out += """
            };\n"""
        return unindent(out, 4)

    def _gen_unify_nixpkgs_keys(self, master_key: str, nixpkgs_keys: List[str]):
        out = ''
        for key in nixpkgs_keys:
            out += f"""        {key} = {master_key};\n"""
        return out

    def _gen_overlays(self, pkgs: Dict[str, ResolvedPkg], overlay_keys):
        out = f"""
            overlay = self: super: {{
              {self.py_ver_nix} = super.{self.py_ver_nix}.override {{
                packageOverrides = python-self: python-super: rec {{
          """
        out = unindent(out, 10)
        for pkg in pkgs.values():
            if pkg.name not in overlay_keys:
                continue
            overlays_required = True
            # get correct build input names
            _build_inputs = [self._get_ref_name(b, pkgs[b].ver) for b in pkg.build_inputs]
            build_inputs_local = {b for b in _build_inputs if b in overlay_keys}
            build_inputs_nixpkgs = set(_build_inputs) - build_inputs_local
            # get correct propagated build input names
            _prop_build_inputs = [self._get_ref_name(b, pkgs[b].ver) for b in pkg.prop_build_inputs]
            prop_build_inputs_local = {b for b in _prop_build_inputs if b in overlay_keys}
            prop_build_inputs_nixpkgs = set(_prop_build_inputs) - prop_build_inputs_local
            # convert build inputs to string
            build_inputs_str = self._gen_build_inputs(build_inputs_local, build_inputs_nixpkgs, ).strip()
            # convert prop build inputs to string
            prop_build_inputs_str = self._gen_prop_build_inputs(prop_build_inputs_local,
                                                                prop_build_inputs_nixpkgs).strip()
            # generate package overlays either via `overrideAttrs` if package already exists in nixpkgs,
            # or by creating it from scratch using `buildPythonPackage`
            if self.nixpkgs.exists(pkg.name):
                nix_name = self.nixpkgs.find_best_nixpkgs_candidate(pkg.name, pkg.ver)
                out += self._gen_overrideAttrs(pkg.name, pkg.ver, nix_name, build_inputs_str, prop_build_inputs_str)
                master_key = self._get_ref_name(pkg.name, pkg.ver)
                other_names = (p.nix_key for p in self.nixpkgs.get_all_candidates(pkg.name) if p.nix_key != master_key)
                out += self._gen_unify_nixpkgs_keys(master_key, sorted(other_names))
            else:
                out += self._gen_builPythonPackage(pkg.name, pkg.ver, build_inputs_str, prop_build_inputs_str)
        end_overlay_section = f"""
                }};
              }};
            }};
          """
        return out + unindent(end_overlay_section, 10)

    def _get_ref_name(self, name, ver) -> str:
        if self.nixpkgs.exists(name):
            return self.nixpkgs.find_best_nixpkgs_candidate(name, ver)
        return name

    def _needs_overlay(self, name, ver):
        """
        We need to generate an overlay if
            1. a specific candidate does not exist in nixpkgs. (We will build it from scratch via buildPythonPackage)
            2. there are multiple candidates with the same name in nixpkgs. We would risk a python package collision
               if we don't override all of them to the same version since some sub dependency
               in nixpkgs might point to one of these other versions.
        """
        return not self.nixpkgs.exists(name, ver) or self.nixpkgs.has_multiple_candidates(name)

    def _gen_python_env(self, pkgs: Dict[str, ResolvedPkg]):
        overlay_keys = {p.name for p in pkgs.values() if self._needs_overlay(p.name, p.ver)}
        out = self._gen_imports() + self._gen_overlays(pkgs, overlay_keys)
        pkg_names = "".join((f"{self._get_ref_name(name, pkgs[name].ver)}\n{' ' * 14}" for (name, pkg) in pkgs.items() if pkg.is_root))
        python_with_packages = f"""
            in
            
            with import nixpkgs_src {{ overlays = [ overlay ]; }};
            
            {self.py_ver_nix}.withPackages (ps: with ps; [
              {pkg_names.rstrip()}
            ])
            """
        return out + unindent(python_with_packages, 12)
