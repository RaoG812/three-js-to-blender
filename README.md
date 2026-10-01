# Three.js to Blender

This Blender extension brings a Three.js scene into Blender as editable Blender
objects. It is useful when you have built a scene in a Three.js app and want to
render it, inspect it, or continue working on it in Blender.

The addon transfers the scene's visible structure and geometry. It does not
translate JavaScript or recreate the Three.js application itself.

## How it works

Three.js scenes are made from objects, geometry, materials, textures, lights,
cameras, and transforms. The addon reads a serialized scene and rebuilds the
parts Blender supports as Blender objects and data.

The important idea for procedural geometry is that a shape becomes its actual
mesh data. For example, a Three.js `BoxGeometry` is generated from parameters
such as width and segment count. The export helper copies its generated vertex
positions, indices, normals, UVs, colors, and material groups into a plain
`BufferGeometry`. Blender then imports those attributes as a mesh. This means
the result is editable mesh geometry in Blender, rather than a live Three.js
`BoxGeometry` generator with its original parameters and JavaScript behavior.

The same approach works for other geometries that already expose a
`BufferGeometry`. The helper clones the scene hierarchy and canonicalizes its
geometry before serialization; it does not modify the running scene.

## Basic workflow

1. In your Three.js application, import `downloadThreeJSON` from
   `three_export_helper.js` and call it with the scene and the Three.js module:

   ```js
   import * as THREE from 'three';
   import { downloadThreeJSON } from './three_export_helper.js';

   downloadThreeJSON(scene, THREE, 'my-scene.three.json');
   ```

2. In Blender, use **File > Import > Three.js Scene** and select the exported
   JSON file. You can also drag a `.json` or `.three.json` file into the 3D
   View.

For React Three Fiber, get the mounted scene with `useThree()` and call the
helper from an event handler:

```tsx
const { scene } = useThree();
<button onClick={() => downloadThreeJSON(scene, THREE)}>Export scene</button>
```

The helper is recommended because some Three.js geometry classes serialize as
constructor parameters, while this importer expects raw vertex attributes. If
you serialize data yourself, provide geometry in the raw `BufferGeometry`
format. Keep external image files next to the JSON or use embedded image data;
the importer does not download remote texture URLs.

## Live preview

The **Three.js** tab in Blender's 3D View can start a local web project and
capture its rendered Three.js scene. Choose the project folder, set the preview
URL if needed, and click **Start Project Preview**. Vite, Next.js 15.3+, and
Create React App development projects receive a temporary renderer hook.
Other app setups may need a runtime hookup using `sendThreeScene()` or
`startThreeSceneSync()` from the helper. When a scene is ready, click
**Import Latest Scene** in Blender.

Live capture sends scene snapshots to a local Blender receiver. Transform
changes are sampled for animation over a rolling window of up to 24 seconds.
Only motion captured after preview starts can be imported. The receiver uses a
local bearer token; the Vite bridge handles it automatically.

Imported transform animation becomes looping Blender actions. Use Blender's
timeline Play control to view motion; the runtime panel reports captured
samples and generated curves after each import.

## Materials and custom shaders

Standard Three.js material properties and supported texture maps are rebuilt
with Blender shader nodes. Custom `ShaderMaterial` and `RawShaderMaterial`
programs and uniforms are retained on the Blender material as custom properties;
common color and Fresnel patterns receive a best-effort node approximation.
Arbitrary GLSL, post-processing effects, and renderer-specific shader behavior
cannot be translated into equivalent Blender nodes automatically.

## What is imported

- Scene hierarchy, object transforms, and Three.js Y-up to Blender Z-up
  conversion
- Triangle meshes from indexed or non-indexed geometry, with UVs, vertex
  colors, normals, and material groups
- Line objects and point clouds
- Common material values and image maps, including PBR properties
- Perspective and orthographic cameras, and common light types
- Scene background, runtime camera, renderer metadata, and object `userData`
- Transform animation clips and sampled live transform animation
- glTF 2.0 scenes (`.gltf` and `.glb`) through Blender's native importer

Most imported elements are normal Blender data and can be edited there. The
source object's serialized JSON is also retained in custom properties for
reference.

## Limits

The addon imports scene data, not source code. TSX/JSX components and HTML
pages must run in their original application to create a scene before export.
Arbitrary shader graphs, DOM labels, fog and tone-mapping appearance, skinning,
morph targets, instancing, and material or shader animation are not fully
recreated. A `SkinnedMesh` is imported as a static mesh. Three.js parametric
geometry JSON must be canonicalized by the helper before import.

## Install

Install `three-js-to-b3d-v1.21.zip` through **Edit > Preferences > Extensions
> Install from Disk**, then enable **Three.js JSON Importer**. Use Blender 4.2
or later.

## Development

From the repository root, validate or build the extension with Blender:

```sh
blender --command extension validate
blender --command extension build
```
