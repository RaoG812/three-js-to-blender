/*
Three.js -> Blender JSON export helper.

Why this exists:
Three.js primitive subclasses (BoxGeometry, SphereGeometry, etc.) may serialize
as constructor parameters instead of raw BufferGeometry attributes. The Blender
importer intentionally consumes the stable raw BufferGeometry structure.

This helper clones your hierarchy and replaces every mesh/line/points geometry
with a plain THREE.BufferGeometry copy before calling Object3D.toJSON().

Usage:
    import * as THREE from 'three';
    import { downloadThreeJSON } from './three_export_helper.js';

    downloadThreeJSON(scene, THREE, 'scene.three.json');
*/

export function canonicalizeGeometry(source, THREE) {
  if (!source || !source.isBufferGeometry) return source;

  const target = new THREE.BufferGeometry();

  if (source.index) {
    target.setIndex(source.index.clone());
  }

  for (const [name, attribute] of Object.entries(source.attributes || {})) {
    target.setAttribute(name, attribute.clone());
  }

  for (const [name, targets] of Object.entries(source.morphAttributes || {})) {
    target.morphAttributes[name] = targets.map((attribute) => attribute.clone());
  }

  target.morphTargetsRelative = !!source.morphTargetsRelative;

  for (const group of source.groups || []) {
    target.addGroup(group.start, group.count, group.materialIndex ?? 0);
  }

  if (source.drawRange) {
    target.setDrawRange(source.drawRange.start, source.drawRange.count);
  }

  target.name = source.name || '';
  target.userData = (typeof structuredClone === 'function')
    ? structuredClone(source.userData || {})
    : JSON.parse(JSON.stringify(source.userData || {}));

  return target;
}

export function sceneToBlenderJSON(root, THREE, { activeCamera, renderer, transformSamples } = {}) {
  // clone(true) preserves the scene hierarchy. Geometry and materials remain
  // shared at this point, so geometry is explicitly replaced below.
  const clone = root.clone(true);
  // Three.js Object3D.clone() creates new UUIDs. Runtime transform samples and
  // animation tracks target the live objects' UUIDs, so retain those identities
  // in the serialized copy used by Blender.
  const sourceObjects = [];
  const clonedObjects = [];
  root.traverse((object) => sourceObjects.push(object));
  clone.traverse((object) => clonedObjects.push(object));
  for (let index = 0; index < Math.min(sourceObjects.length, clonedObjects.length); index += 1) {
    clonedObjects[index].uuid = sourceObjects[index].uuid;
  }
  if (activeCamera?.uuid) {
    clone.userData = { ...clone.userData, __threeBlenderActiveCamera: activeCamera.uuid };
  }
  if (renderer) {
    let clearColor = null;
    try {
      clearColor = renderer.getClearColor(new THREE.Color()).getHex();
    } catch {}
    clone.userData = {
      ...clone.userData,
      __threeBlenderRenderer: {
        clearColor,
        clearAlpha: renderer.getClearAlpha?.() ?? 1,
        toneMapping: renderer.toneMapping,
        toneMappingExposure: renderer.toneMappingExposure,
        outputColorSpace: renderer.outputColorSpace,
        shadowMapEnabled: renderer.shadowMap?.enabled ?? false,
        shadowMapType: renderer.shadowMap?.type,
      },
    };
  }
  if (transformSamples?.samples?.length) {
    clone.userData = { ...clone.userData, __threeBlenderTransformSamples: transformSamples };
  }

  const imageSources = new Map();
  clone.traverse((object) => {
    if (object.geometry?.isBufferGeometry) {
      object.geometry = canonicalizeGeometry(object.geometry, THREE);
    }
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) {
      if (!material) continue;
      for (const value of Object.values(material)) {
        if (value?.isTexture && value.source?.uuid && value.source.data) {
          imageSources.set(value.source.uuid, value.source.data);
        }
      }
    }
  });

  const json = clone.toJSON();
  for (const image of json.images || []) {
    const source = imageSources.get(image.uuid);
    if (!source || Array.isArray(source)) continue;
    try {
      if (typeof source.toDataURL === 'function') {
        image.url = source.toDataURL('image/png');
      } else {
        const canvas = document.createElement('canvas');
        canvas.width = source.naturalWidth || source.videoWidth || source.width;
        canvas.height = source.naturalHeight || source.videoHeight || source.height;
        if (!canvas.width || !canvas.height) continue;
        canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
        image.url = canvas.toDataURL('image/png');
      }
    } catch (error) {
      console.warn(`Could not embed Three.js texture ${image.uuid}; keeping its source URL.`, error);
    }
  }
  return json;
}

export function downloadThreeJSON(root, THREE, filename = 'scene.three.json') {
  const json = sceneToBlenderJSON(root, THREE);
  const blob = new Blob([JSON.stringify(json, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();

  URL.revokeObjectURL(url);
}

// Convenient alias for app code that wants to name the action explicitly.
export function exportThreeScene(root, THREE) {
  return sceneToBlenderJSON(root, THREE);
}

/** Send a live scene to the Blender add-on's local runtime receiver. */
export async function sendThreeScene(
  root,
  THREE,
  { url = 'http://127.0.0.1:8765/scene', token, name = 'Live Three.js Scene', getActiveCamera, getRenderer, getTransformSamples } = {},
) {
  if (!token) throw new Error('A Blender runtime receiver token is required.');
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      'X-Three-Scene-Name': name,
    },
    body: JSON.stringify(sceneToBlenderJSON(root, THREE, {
      activeCamera: getActiveCamera?.(),
      renderer: getRenderer?.(),
      transformSamples: getTransformSamples?.(),
    })),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.error || `Blender receiver returned HTTP ${response.status}`);
  }
  return result;
}

/** Keep Blender's live preview refreshed while the app is running. */
export function startThreeSceneSync(
  root,
  THREE,
  { intervalMs = 1500, onError = (error) => console.error(error), onAnimationHistory = () => {}, ...options } = {},
) {
  let active = true;
  let sending = false;
  const transformHistory = { startedAt: performance.now(), samples: [] };
  const sampleTransforms = () => {
    const currentRoot = typeof root === 'function' ? root() : root;
    if (!currentRoot?.isObject3D) return;
    const objects = {};
    currentRoot.traverse((object) => {
      if (object === currentRoot || !object.uuid) return;
      objects[object.uuid] = {
        position: object.position.toArray(),
        rotation: object.rotation.toArray().slice(0, 3),
        scale: object.scale.toArray(),
      };
    });
    transformHistory.samples.push({ time: (performance.now() - transformHistory.startedAt) / 1000, objects });
    if (transformHistory.samples.length > 240) transformHistory.samples.shift();
    if (transformHistory.samples.length === 240 || transformHistory.samples.length % 10 === 0) {
      onAnimationHistory(transformHistory.samples.length);
    }
  };
  sampleTransforms();
  const sampleTimer = setInterval(sampleTransforms, 100);
  const sync = async () => {
    if (!active || sending) return;
    sending = true;
    try {
      const currentRoot = typeof root === 'function' ? root() : root;
      if (!currentRoot || !currentRoot.isObject3D) {
        throw new Error('The scene expression must return a Three.js Object3D or Scene.');
      }
      await sendThreeScene(currentRoot, THREE, {
        ...options,
        getTransformSamples: () => transformHistory,
      });
    } catch (error) {
      onError(error);
    } finally {
      sending = false;
    }
  };
  void sync();
  const timer = setInterval(sync, Math.max(500, intervalMs));
  return () => {
    active = false;
    clearInterval(timer);
    clearInterval(sampleTimer);
  };
}
