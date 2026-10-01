# Repository Guidelines

## Project Structure & Module Organization

This repository is a Blender extension for importing Three.js JSON. The root contains the extension manifest (`blender_manifest.toml`), registration entry point (`__init__.py`), Blender importer (`importer.py`), browser-side Three.js export helper (`three_export_helper.js`), and sample input (`sample_scene.three.json`). `README.md` documents supported data and limitations. There is no separate test or asset directory; keep focused fixtures alongside the sample or add dedicated directories when they become necessary.

## Build, Test, and Development Commands

Run Blender's extension commands from the repository root with Blender 4.2 or later available on `PATH`:

- `blender --command extension validate` checks the extension layout and manifest.
- `blender --command extension build` packages the extension as an installable archive.

To develop, install the unpacked extension in Blender and import `sample_scene.three.json` through **File > Import > Three.js JSON**. The JavaScript helper is an ES module intended for use in a Three.js application; this repository does not define an npm build or test command.

## Coding Style & Naming Conventions

Follow the existing language conventions: Python uses four-space indentation, `snake_case` functions and variables, and uppercase constants; JavaScript uses two-space indentation, `camelCase` functions, and semicolons. Keep importer logic compatible with Blender's bundled Python and use Blender APIs through `bpy` and `mathutils`. Preserve the export/import contract: Three.js UUID references and raw BufferGeometry attributes are expected by the importer.

## Testing Guidelines

No automated test framework or coverage requirement is configured. For importer changes, validate the extension and perform a Blender import using the sample JSON; include cases for affected geometry, transforms, or material behavior when practical. For export-helper changes, exercise the helper with a Three.js scene and confirm its serialized geometries contain raw attributes.

## Commit & Pull Request Guidelines

The available Git history contains only the initial commit, so no established commit-message pattern can be inferred. Use short, imperative subjects (for example, `Support vertex color attributes`). Pull requests should describe the user-visible change, list validation performed, and include a sample JSON or Blender screenshot when the change affects imported output. Call out changes to supported Blender versions or manifest metadata.

## Security & Configuration

Do not commit private scene data, credentials, or generated ZIP artifacts unless they are intentional release assets. Keep extension version and minimum Blender version in `blender_manifest.toml` aligned with release documentation.
