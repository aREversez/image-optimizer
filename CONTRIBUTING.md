# Contributing

Contributions are welcome! Please follow these guidelines:

## Reporting Issues

- Check existing issues before opening a new one
- Include steps to reproduce, expected behavior, and actual behavior
- Attach screenshots or logs if relevant

## Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Run `pip install -r requirements.txt` to install dependencies
5. Test manually by running `python -m image_optimizer`
6. Ensure no new warnings or errors appear in the console or browser dev tools
7. Submit a PR with a clear description of the change

## Code Style

- Python: follow PEP 8, use type hints
- JavaScript: modern ES6+, no external libraries
- CSS: use CSS custom properties (variables) from `:root`
- No commented-out code; delete what you don't need

## Releasing (maintainers)

1. Bump `__version__` in `image_optimizer/__init__.py` (`pyproject.toml` reads it, so it has no version of its own) and move the `[Unreleased]` entries in `CHANGELOG.md` under the new version
2. Optional dry run: run the **Release** workflow from the Actions tab and check the artifacts it produces. To check the PyPI distribution locally: `python -m build && twine check --strict dist/*`
3. Tag and push: `git tag v1.2.3 && git push origin v1.2.3`. The tag must equal `__version__` (a leading `v` or `V` is ignored), otherwise the **Publish** workflow fails. Two workflows run on the tag:
   - **Publish** builds the sdist and wheel, checks them, and uploads them to PyPI as `image-optimizer-webui` through Trusted Publishing (the `pypi` environment; there is no API token). PyPI never accepts a version twice, so a bad upload can only be yanked and replaced by a new version
   - **Release** builds the Windows, macOS and Linux downloads and attaches them to a GitHub Release for the tag
4. To build locally instead: `python scripts/fetch_tools.py && python build_exe.py` (PyInstaller builds only for the OS it runs on)

Before the first PyPI release, register `publish.yml` with the environment `pypi` as a pending publisher on pypi.org, and create the `pypi` environment under the repository's Settings → Environments.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
