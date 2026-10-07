bl_info = {
    "name": "Three.js JSON Importer",
    "author": "Local Developer",
    "version": (1, 21, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > Three.js JSON (.json)",
    "description": "Import Three.js Object3D/Scene JSON and BufferGeometry JSON",
    "category": "Import-Export",
}

from .importer import register, unregister

if __name__ == "__main__":
    register()
