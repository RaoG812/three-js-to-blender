import json
import math
import os
import base64
import re
import tempfile
import urllib.parse
import hmac
import hashlib
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Euler, Matrix, Quaternion, Vector


# Three.js is right-handed and defaults to Y-up.
# Blender is right-handed and Z-up.
# +90 degrees around X maps:
#   Three +Y -> Blender +Z
#   Three +Z -> Blender -Y
C_THREE_TO_BLENDER = Matrix((
    (1.0,  0.0,  0.0, 0.0),
    (0.0,  0.0, -1.0, 0.0),
    (0.0,  1.0,  0.0, 0.0),
    (0.0,  0.0,  0.0, 1.0),
))
C_BLENDER_TO_THREE = C_THREE_TO_BLENDER.inverted()

_RUNTIME_QUEUE = queue.Queue()
_RUNTIME_SERVER = None
_RUNTIME_TOKEN = ""
_RUNTIME_PORT = 0
_RUNTIME_LAST_ERROR = ""
_RUNTIME_LAST_STATUS = ""
_RUNTIME_LAST_IMPORT_REPORT = ""
_RUNTIME_MAX_BYTES = 128 * 1024 * 1024
_RUNTIME_PREVIEW_COLLECTION = "Three.js Live Preview"
_PREVIEW_PROCESS = None
_PREVIEW_LOG = None
_PREVIEW_OPEN_URL = ""
_PREVIEW_INSTALLING = False
_PREVIEW_LOG_PATH = ""
_PREVIEW_GENERATED_FILES = []
_PREVIEW_MODIFIED_FILES = []
_PREVIEW_CAPTURE_MODE = ""


def _chunks(values, n):
    return [values[i:i+n] for i in range(0, len(values), n)]


def _three_matrix(values):
    """Convert Three.js Matrix4.toArray() column-major data to mathutils.Matrix."""
    if not values or len(values) != 16:
        return Matrix.Identity(4)
    return Matrix((
        (values[0], values[4], values[8],  values[12]),
        (values[1], values[5], values[9],  values[13]),
        (values[2], values[6], values[10], values[14]),
        (values[3], values[7], values[11], values[15]),
    ))


def _node_local_matrix(node):
    values = node.get("matrix")
    if isinstance(values, list) and len(values) == 16:
        return _three_matrix(values)

    position = node.get("position", [0.0, 0.0, 0.0])
    scale = node.get("scale", [1.0, 1.0, 1.0])

    # Three.js quaternion JSON order is x, y, z, w.
    q = node.get("quaternion")
    if isinstance(q, list) and len(q) == 4:
        rotation = Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
    else:
        rot = node.get("rotation", [0.0, 0.0, 0.0])
        order = node.get("rotationOrder", "XYZ")
        try:
            rotation = Euler(tuple(rot[:3]), order).to_matrix().to_4x4()
        except Exception:
            rotation = Euler(tuple(rot[:3]), "XYZ").to_matrix().to_4x4()

    translation = Matrix.Translation(Vector(position[:3]))
    scaling = Matrix.Diagonal(Vector((scale[0], scale[1], scale[2], 1.0)))
    return translation @ rotation @ scaling


def _convert_matrix(m, convert_axes=True, global_scale=1.0):
    out = (C_THREE_TO_BLENDER @ m @ C_BLENDER_TO_THREE) if convert_axes else m.copy()
    out.translation *= global_scale
    return out


def _convert_vec3(v, convert_axes=True, global_scale=1.0, direction=False):
    vec = Vector((float(v[0]), float(v[1]), float(v[2])))
    if convert_axes:
        vec = C_THREE_TO_BLENDER.to_3x3() @ vec
    if not direction:
        vec *= global_scale
    return tuple(vec)


def _geometry_payload(geometry):
    """
    Return canonical BufferGeometry-like data:
      {
        attributes: {...},
        index: {...} | None,
        groups: [...]
      }

    Supported directly:
    - BufferGeometry.toJSON()
    - full Object3D/Scene JSON geometries containing raw `data`
    """
    if not isinstance(geometry, dict):
        return None

    data = geometry.get("data")
    if isinstance(data, dict) and isinstance(data.get("attributes"), dict):
        return data

    # Some custom exporters may put attributes at the top level.
    if isinstance(geometry.get("attributes"), dict):
        return geometry

    return None


def _attribute_array(attr):
    if not isinstance(attr, dict):
        return [], 0

    arr = attr.get("array", [])
    item_size = int(attr.get("itemSize", 0) or 0)

    # Be permissive toward custom serializers.
    if isinstance(arr, dict):
        arr = arr.get("array", arr.get("data", []))

    return list(arr or []), item_size


def _build_topology(payload, object_type):
    attrs = payload.get("attributes", {})
    positions, pos_size = _attribute_array(attrs.get("position", {}))
    if pos_size < 3 or not positions:
        raise ValueError("Geometry has no valid position attribute")

    vertex_count = len(positions) // pos_size
    index_values = []
    index = payload.get("index")
    if isinstance(index, dict):
        index_values, _ = _attribute_array(index)
    elif isinstance(index, list):
        index_values = index

    sequence = [int(i) for i in index_values] if index_values else list(range(vertex_count))

    faces = []
    edges = []

    if object_type in {"Line", "LineLoop"}:
        edges = [(sequence[i], sequence[i + 1]) for i in range(len(sequence) - 1)]
        if object_type == "LineLoop" and len(sequence) > 2:
            edges.append((sequence[-1], sequence[0]))
    elif object_type == "LineSegments":
        edges = [(sequence[i], sequence[i + 1]) for i in range(0, len(sequence) - 1, 2)]
    elif object_type == "Points":
        pass
    else:
        # Three.js BufferGeometry groups and draw modes for normal Meshes use triangles.
        faces = [
            (sequence[i], sequence[i + 1], sequence[i + 2])
            for i in range(0, len(sequence) - 2, 3)
        ]

    return positions, pos_size, vertex_count, faces, edges, bool(index_values)


def _safe_material_name(mat_json):
    return mat_json.get("name") or mat_json.get("type") or "ThreeMaterial"


def _hex_rgb(value):
    try:
        value = int(value)
    except Exception:
        value = 0xCCCCCC
    def srgb_to_linear(channel):
        channel /= 255.0
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    return tuple(srgb_to_linear((value >> shift) & 255) for shift in (16, 8, 0))


def _socket(bsdf, *names):
    for name in names:
        sock = bsdf.inputs.get(name)
        if sock is not None:
            return sock
    return None


def _make_material(mat_json):
    mat = bpy.data.materials.new(_safe_material_name(mat_json))
    mat.use_nodes = True

    unlit_material = mat_json.get("type") in {"MeshBasicMaterial", "LineBasicMaterial", "PointsMaterial"}
    is_shader_material = mat_json.get("type") in {"ShaderMaterial", "RawShaderMaterial"}
    shader_color = None
    shader_uniforms = mat_json.get("uniforms", {})
    if is_shader_material and isinstance(shader_uniforms, dict):
        for uniform_name in ("glowColor", "emissiveColor", "uColor", "color"):
            uniform = shader_uniforms.get(uniform_name)
            if isinstance(uniform, dict) and uniform.get("type") == "c" and isinstance(uniform.get("value"), (int, float)):
                shader_color = int(uniform["value"])
                break
    color = _hex_rgb(mat_json.get("color", shader_color if shader_color is not None else 0x050505 if is_shader_material else 0xCCCCCC))
    opacity = float(mat_json.get("opacity", 1.0))
    shader_source = " ".join(str(mat_json.get(key, "")) for key in ("vertexShader", "fragmentShader"))
    shader_alpha_match = re.search(r"(?:\*\s*|,\s*)(0?\.\d+)\s*\)?\s*;?\s*}\s*$", shader_source)
    if is_shader_material and mat_json.get("transparent") and shader_alpha_match:
        opacity = min(opacity, float(shader_alpha_match.group(1)))
    mat.diffuse_color = (*color, opacity)

    nodes = mat.node_tree.nodes
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        sock = _socket(bsdf, "Base Color")
        if sock and not (unlit_material or is_shader_material):
            sock.default_value = (*color, 1.0)

        sock = _socket(bsdf, "Metallic")
        if sock and "metalness" in mat_json:
            sock.default_value = float(mat_json["metalness"])

        sock = _socket(bsdf, "Roughness")
        if sock and "roughness" in mat_json:
            sock.default_value = float(mat_json["roughness"])

        sock = _socket(bsdf, "Alpha")
        if sock:
            sock.default_value = opacity

        if "emissive" in mat_json:
            emissive = _hex_rgb(mat_json["emissive"])
            esock = _socket(bsdf, "Emission Color", "Emission")
            if esock:
                esock.default_value = (*emissive, 1.0)
            strength = float(mat_json.get("emissiveIntensity", 1.0))
            ssock = _socket(bsdf, "Emission Strength")
            if ssock:
                ssock.default_value = strength
        elif unlit_material:
            base = _socket(bsdf, "Base Color")
            if base:
                base.default_value = (0.0, 0.0, 0.0, 1.0)
            emission = _socket(bsdf, "Emission Color", "Emission")
            if emission:
                emission.default_value = (*color, 1.0)
            strength = _socket(bsdf, "Emission Strength")
            if strength:
                strength.default_value = 1.0
        elif is_shader_material and shader_color is not None:
            base = _socket(bsdf, "Base Color")
            if base:
                base.default_value = (0.0, 0.0, 0.0, 1.0)
            emission = _socket(bsdf, "Emission Color", "Emission")
            if emission:
                emission.default_value = (*_hex_rgb(shader_color), 1.0)
            strength = _socket(bsdf, "Emission Strength")
            if strength:
                strength.default_value = 1.0

        scalar_properties = {
            "clearcoat": ("Coat Weight", "Clearcoat"),
            "clearcoatRoughness": ("Coat Roughness", "Clearcoat Roughness"),
            "sheen": ("Sheen Weight",),
            "sheenRoughness": ("Sheen Roughness",),
            "specularIntensity": ("Specular IOR Level", "Specular"),
            "ior": ("IOR",),
            "transmission": ("Transmission Weight", "Transmission"),
            "thickness": ("Thickness",),
            "iridescence": ("Iridescence Weight",),
        }
        for prop, sockets in scalar_properties.items():
            value_socket = _socket(bsdf, *sockets)
            if value_socket is not None and prop in mat_json:
                value_socket.default_value = float(mat_json[prop])

        color_properties = {
            "sheenColor": ("Sheen Tint", "Sheen Color"),
            "specularColor": ("Specular Tint", "Specular Color"),
        }
        for prop, sockets in color_properties.items():
            color_socket = _socket(bsdf, *sockets)
            if color_socket is not None and prop in mat_json:
                color_socket.default_value = (*_hex_rgb(mat_json[prop]), 1.0)

        if mat_json.get("side") == 2:
            mat.use_backface_culling = False
        elif is_shader_material:
            # Keep custom shader shells visible from either side in the Blender
            # approximation; the original side/blending settings remain stored.
            mat.use_backface_culling = False
        elif "side" in mat_json:
            mat.use_backface_culling = True
        if is_shader_material and shader_color is not None and re.search(r"pow\s*\(\s*1(?:\.0)?\s*-\s*dot\s*\(", shader_source):
            geometry = nodes.new("ShaderNodeNewGeometry")
            dot = nodes.new("ShaderNodeVectorMath")
            dot.operation = "DOT_PRODUCT"
            mat.node_tree.links.new(geometry.outputs["Normal"], dot.inputs[0])
            mat.node_tree.links.new(geometry.outputs["Incoming"], dot.inputs[1])
            absolute_dot = nodes.new("ShaderNodeMath")
            absolute_dot.operation = "ABSOLUTE"
            mat.node_tree.links.new(dot.outputs["Value"], absolute_dot.inputs[0])
            rim = nodes.new("ShaderNodeMath")
            rim.operation = "SUBTRACT"
            rim.inputs[0].default_value = 1.0
            mat.node_tree.links.new(absolute_dot.outputs[0], rim.inputs[1])
            power_match = re.search(r"pow\s*\(\s*1(?:\.0)?\s*-\s*dot\s*\([^)]*\)\s*,\s*([0-9.]+)", shader_source)
            if power_match:
                power = nodes.new("ShaderNodeMath")
                power.operation = "POWER"
                power.inputs[1].default_value = float(power_match.group(1))
                mat.node_tree.links.new(rim.outputs[0], power.inputs[0])
                alpha_source = power.outputs[0]
            else:
                alpha_source = rim.outputs[0]
            alpha_scale = nodes.new("ShaderNodeMath")
            alpha_scale.operation = "MULTIPLY"
            alpha_scale.inputs[1].default_value = opacity
            mat.node_tree.links.new(alpha_source, alpha_scale.inputs[0])
            alpha_socket = _socket(bsdf, "Alpha")
            if alpha_socket:
                mat.node_tree.links.new(alpha_scale.outputs[0], alpha_socket)
            mat["three_shader_approximation"] = "GLSL Fresnel rim approximated with Layer Weight, Principled emission, and alpha."
        if mat_json.get("wireframe"):
            mat["three_wireframe"] = True

    # Blender 4.x and 5.x have changed transparency settings over time.
    if opacity < 1.0 or mat_json.get("transparent", False) or mat_json.get("alphaMap"):
        if hasattr(mat, "surface_render_method"):
            try:
                mat.surface_render_method = "DITHERED"
            except Exception:
                pass
        if hasattr(mat, "blend_method"):
            try:
                mat.blend_method = "BLEND"
            except Exception:
                pass

    mat["three_type"] = mat_json.get("type", "")
    # Keep the original program and uniforms available even when Blender cannot
    # reproduce a custom GLSL material's renderer-specific behavior.
    for source_key in ("vertexShader", "fragmentShader"):
        source = mat_json.get(source_key)
        if isinstance(source, str) and source:
            mat[f"three_{source_key}"] = source
    if isinstance(shader_uniforms, dict):
        try:
            mat["three_shader_uniforms"] = json.dumps(shader_uniforms, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    if unlit_material:
        mat["three_unlit"] = True
    mat["three_uuid"] = mat_json.get("uuid", "")
    for prop in ("flatShading", "depthWrite", "depthTest", "alphaTest", "vertexColors", "ior", "transmission", "thickness", "attenuationDistance"):
        if prop in mat_json:
            try:
                mat[f"three_{prop}"] = mat_json[prop]
            except Exception:
                pass
    return mat


def _texture_source_path(url, filepath):
    """Resolve a Three.js image URL without fetching arbitrary remote content."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        match = re.match(r"data:([^;,]+);base64,(.*)", url, re.DOTALL)
        if not match:
            return None
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(match.group(1), ".img")
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
                temp.write(base64.b64decode(match.group(2)))
                return temp.name
        except Exception:
            return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("", "file"):
        return None
    path = urllib.parse.unquote(parsed.path) if parsed.scheme else url.split("?", 1)[0]
    if parsed.scheme == "file" and parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(filepath), path)
    return os.path.normpath(path)


def _wire_texture(mat, bsdf, slot, texture_json, image_json, filepath):
    image_url = image_json.get("url")
    image = None
    if isinstance(image_url, str) and image_url.startswith("data:"):
        image_key = "ThreeData_" + hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:20]
        image = bpy.data.images.get(image_key)
    image_path = None if image else _texture_source_path(image_url, filepath)
    if not image:
        if not image_path or not os.path.isfile(image_path):
            return
        try:
            image = bpy.data.images.load(image_path, check_existing=True)
            if image_url.startswith("data:"):
                image.name = image_key
            image.pack()
        except Exception:
            return
        finally:
            if image_path and isinstance(image_url, str) and image_url.startswith("data:"):
                try:
                    os.unlink(image_path)
                except OSError:
                    pass
    nodes = mat.node_tree.nodes
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.label = slot
    tex.name = f"Three.js {slot}"
    tex["three_texture_json"] = json.dumps(texture_json, ensure_ascii=False)
    try:
        image.colorspace_settings.name = "sRGB" if slot in {"map", "emissiveMap", "specularColorMap", "sheenColorMap"} else "Non-Color"
    except Exception:
        pass
    wrapping = texture_json.get("wrap", ["RepeatWrapping", "RepeatWrapping"])
    if isinstance(wrapping, list) and wrapping:
        tex.extension = "CLIP" if wrapping[0] in (1001, "ClampToEdgeWrapping") else "REPEAT"
    texture_node = texture_json
    repeat = texture_node.get("repeat", [1, 1])
    offset = texture_node.get("offset", [0, 0])
    rotation = float(texture_node.get("rotation", 0.0))
    channel = int(texture_node.get("channel", 0) or 0)
    uv = nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap" if channel == 0 else f"UVMap.{channel:03d}"
    if len(repeat) >= 2 and len(offset) >= 2 and (list(repeat[:2]) != [1, 1] or list(offset[:2]) != [0, 0] or rotation):
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Location"].default_value = (float(offset[0]), float(offset[1]), 0.0)
        mapping.inputs["Scale"].default_value = (float(repeat[0]), float(repeat[1]), 1.0)
        mapping.inputs["Rotation"].default_value[2] = rotation
        mat.node_tree.links.new(uv.outputs["UV"], mapping.inputs["Vector"])
        mat.node_tree.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    else:
        mat.node_tree.links.new(uv.outputs["UV"], tex.inputs["Vector"])

    targets = {
        "map": ("Base Color", "Color"), "emissiveMap": ("Emission Color", "Emission"),
        "normalMap": ("Normal",), "roughnessMap": ("Roughness",), "metalnessMap": ("Metallic",),
        "aoMap": (), "alphaMap": ("Alpha",),
        "clearcoatMap": ("Coat Weight", "Clearcoat"),
        "clearcoatRoughnessMap": ("Coat Roughness", "Clearcoat Roughness"),
        "sheenColorMap": ("Sheen Tint", "Sheen Color"),
        "sheenRoughnessMap": ("Sheen Roughness",),
        "specularMap": ("Specular IOR Level", "Specular"),
        "specularColorMap": ("Specular Tint", "Specular Color"),
        "specularIntensityMap": ("Specular IOR Level",),
        "iridescenceMap": ("Iridescence Weight", "Iridescence"),
        "iridescenceThicknessMap": ("Iridescence Thickness",),
        "transmissionMap": ("Transmission Weight", "Transmission"),
        "thicknessMap": ("Thickness",),
    }
    if mat.get("three_unlit"):
        targets["map"] = ("Emission Color", "Emission")
        targets["emissiveMap"] = ("Emission Color", "Emission")
    socket = _socket(bsdf, *targets.get(slot, ()))
    if slot == "aoMap":
        base_color = _socket(bsdf, "Base Color")
        if base_color:
            links = mat.node_tree.links
            previous = next((link.from_socket for link in links if link.to_socket == base_color), None)
            if previous is not None:
                for link in list(links):
                    if link.to_socket == base_color:
                        links.remove(link)
            else:
                previous = base_color
            multiply = nodes.new("ShaderNodeMixRGB")
            multiply.blend_type = "MULTIPLY"
            multiply.inputs[0].default_value = 1.0
            links.new(previous if hasattr(previous, "node") else tex.outputs["Color"], multiply.inputs[1])
            if previous is base_color:
                multiply.inputs[1].default_value = tuple(base_color.default_value)
                links.new(tex.outputs["Color"], multiply.inputs[2])
            else:
                links.new(tex.outputs["Color"], multiply.inputs[2])
            links.new(multiply.outputs["Color"], base_color)
        return
    if socket:
        output = tex.outputs["Color"]
        if slot == "normalMap":
            normal = nodes.new("ShaderNodeNormalMap")
            material_json = json.loads(mat.get("three_material_json", "{}"))
            normal_scale = material_json.get("normalScale", [1.0, 1.0])
            if isinstance(normal_scale, list) and normal_scale:
                normal.inputs["Strength"].default_value = float(normal_scale[0])
            mat.node_tree.links.new(output, normal.inputs["Color"])
            output = normal.outputs["Normal"]
        mat.node_tree.links.new(output, socket)


def _assign_uvs(mesh, payload):
    attrs = payload.get("attributes", {})
    for channel in range(8):
        attr_name = "uv" if channel == 0 else f"uv{channel}"
        values, item_size = _attribute_array(attrs.get(attr_name, {}))
        if item_size < 2 or not values:
            continue
        layer_name = "UVMap" if channel == 0 else f"UVMap.{channel:03d}"
        uv_layer = mesh.uv_layers.new(name=layer_name)
        for loop in mesh.loops:
            base = loop.vertex_index * item_size
            if base + 1 < len(values):
                uv_layer.data[loop.index].uv = (float(values[base]), float(values[base + 1]))


def _assign_vertex_colors(mesh, payload):
    attrs = payload.get("attributes", {})
    values, item_size = _attribute_array(attrs.get("color", {}))
    if item_size not in {3, 4} or not values:
        return

    try:
        layer = mesh.color_attributes.new(
            name="Color",
            type="FLOAT_COLOR",
            domain="CORNER",
        )
    except Exception:
        return

    for loop in mesh.loops:
        vi = loop.vertex_index
        base = vi * item_size
        if base + item_size <= len(values):
            r = float(values[base])
            g = float(values[base + 1])
            b = float(values[base + 2])
            a = float(values[base + 3]) if item_size == 4 else 1.0
            layer.data[loop.index].color = (r, g, b, a)


def _assign_normals(mesh, payload, convert_axes):
    attrs = payload.get("attributes", {})
    values, item_size = _attribute_array(attrs.get("normal", {}))
    if item_size < 3 or not values:
        return

    normals = []
    for i in range(0, len(values), item_size):
        if i + 2 >= len(values):
            break
        n = _convert_vec3(values[i:i+3], convert_axes=convert_axes, direction=True)
        normals.append(n)

    if len(normals) != len(mesh.vertices):
        return

    for poly in mesh.polygons:
        poly.use_smooth = True

    try:
        mesh.normals_split_custom_set_from_vertices(normals)
    except Exception:
        # Geometry itself still imports correctly if a Blender API version
        # changes custom-normal behavior.
        pass


def _apply_groups(mesh, payload, is_indexed):
    groups = payload.get("groups", [])
    if not isinstance(groups, list) or not groups:
        return

    for group in groups:
        start = int(group.get("start", 0))
        count = int(group.get("count", 0))
        mat_index = int(group.get("materialIndex", 0))
        if count <= 0:
            continue

        # For triangles, each polygon consumes 3 index entries/vertices.
        first_poly = max(0, start // 3)
        last_poly_exclusive = min(len(mesh.polygons), (start + count + 2) // 3)
        for pi in range(first_poly, last_poly_exclusive):
            mesh.polygons[pi].material_index = mat_index


def _build_mesh_datablock(
    name,
    geometry,
    object_type,
    convert_axes=True,
    global_scale=1.0,
):
    payload = _geometry_payload(geometry)
    if payload is None:
        raise ValueError(
            "Geometry is parametric or does not contain raw BufferGeometry attributes. "
            "Use the included three_export_helper.js before exporting."
        )

    positions, pos_size, vertex_count, faces, edges, is_indexed = _build_topology(
        payload, object_type
    )

    verts = []
    for i in range(vertex_count):
        base = i * pos_size
        verts.append(
            _convert_vec3(
                positions[base:base+3],
                convert_axes=convert_axes,
                global_scale=global_scale,
            )
        )

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, edges, faces)
    mesh.update(calc_edges=True)

    _assign_uvs(mesh, payload)
    _assign_vertex_colors(mesh, payload)
    _assign_normals(mesh, payload, convert_axes)
    _apply_groups(mesh, payload, is_indexed)

    mesh["three_uuid"] = geometry.get("uuid", "")
    mesh["three_type"] = geometry.get("type", "BufferGeometry")
    mesh["three_geometry_json"] = json.dumps(geometry, ensure_ascii=False)
    return mesh


def _add_shader_bloom_approximation(scene, materials):
    needs_glow = False
    for material in materials:
        if material.get("three_shader_approximation"):
            needs_glow = True
            break
        try:
            source = json.loads(material.get("three_material_json", "{}"))
            emissive = int(source.get("emissive", 0) or 0)
            needs_glow = emissive != 0 and float(source.get("emissiveIntensity", 0.0)) > 0.0
        except (TypeError, ValueError, json.JSONDecodeError):
            needs_glow = False
        if needs_glow:
            break
    if not needs_glow:
        return
    try:
        scene.use_nodes = True
        tree = scene.node_tree
        render_layers = next((node for node in tree.nodes if node.type == "R_LAYERS"), None)
        composite = next((node for node in tree.nodes if node.type == "COMPOSITE"), None)
        if render_layers is None:
            render_layers = tree.nodes.new("CompositorNodeRLayers")
        if composite is None:
            composite = tree.nodes.new("CompositorNodeComposite")
        glare = tree.nodes.new("CompositorNodeGlare")
        glare.label = "Three.js Bloom Approximation"
        if hasattr(glare, "glare_type"):
            glare.glare_type = "FOG_GLOW"
        if hasattr(glare, "size"):
            glare.size = 8
        if hasattr(glare, "quality"):
            glare.quality = "HIGH"
        if hasattr(glare, "threshold"):
            glare.threshold = 0.8
        threshold_socket = glare.inputs.get("Threshold")
        if threshold_socket:
            threshold_socket.default_value = 0.8

        composite_image = composite.inputs.get("Image")
        previous_link = next((link for link in tree.links if link.to_socket == composite_image), None)
        source = previous_link.from_socket if previous_link else render_layers.outputs.get("Image")
        if previous_link:
            tree.links.remove(previous_link)
        image_input = glare.inputs.get("Image")
        image_output = glare.outputs.get("Image") or glare.outputs[0]
        if source and image_input:
            tree.links.new(source, image_input)
            tree.links.new(image_output, composite_image)
        scene["three_post_effects_note"] = "Added Blender Fog Glow as an approximation for the detected Three.js Fresnel shader/bloom."
    except Exception as exc:
        scene["three_post_effects_note"] = f"Could not create compositor glow approximation: {exc}"


class ThreeJSONImportContext:
    def __init__(
        self,
        context,
        json_data,
        filepath,
        convert_axes=True,
        global_scale=1.0,
        import_materials=True,
        keep_user_data=True,
    ):
        self.context = context
        self.data = json_data
        self.filepath = filepath
        self.convert_axes = convert_axes
        self.global_scale = global_scale
        self.import_materials = import_materials
        self.keep_user_data = keep_user_data

        self.geometry_json = {}
        self.material_json = {}
        self.texture_json = {}
        self.image_json = {}
        self.animation_json = {}
        self.materials = {}
        self.objects_by_uuid = {}
        self.objects_by_name = {}

        for geo in self.data.get("geometries", []) if isinstance(self.data, dict) else []:
            if isinstance(geo, dict) and geo.get("uuid"):
                self.geometry_json[geo["uuid"]] = geo

        for mat in self.data.get("materials", []) if isinstance(self.data, dict) else []:
            if isinstance(mat, dict) and mat.get("uuid"):
                self.material_json[mat["uuid"]] = mat

        for texture in self.data.get("textures", []) if isinstance(self.data, dict) else []:
            if isinstance(texture, dict) and texture.get("uuid"):
                self.texture_json[texture["uuid"]] = texture
        for image in self.data.get("images", []) if isinstance(self.data, dict) else []:
            if isinstance(image, dict) and image.get("uuid"):
                self.image_json[image["uuid"]] = image
        for animation in self.data.get("animations", []) if isinstance(self.data, dict) else []:
            if isinstance(animation, dict) and animation.get("uuid"):
                self.animation_json[animation["uuid"]] = animation

    def load_texture_image(self, texture_uuid):
        texture = self.texture_json.get(texture_uuid)
        image_json = self.image_json.get(texture.get("image")) if texture else None
        image_url = image_json.get("url") if image_json else None
        if not isinstance(image_url, str):
            return None
        image_key = "ThreeData_" + hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:20] if image_url.startswith("data:") else None
        image = bpy.data.images.get(image_key) if image_key else None
        image_path = None if image else _texture_source_path(image_url, self.filepath)
        if not image and image_path and os.path.isfile(image_path):
            try:
                image = bpy.data.images.load(image_path, check_existing=True)
                if image_key:
                    image.name = image_key
                image.pack()
            except Exception:
                image = None
            finally:
                if image_key and image_path:
                    try:
                        os.unlink(image_path)
                    except OSError:
                        pass
        return image

    def apply_scene_settings(self, root_node):
        if root_node.get("type") != "Scene":
            return
        world = bpy.data.worlds.get("Three.js World") or bpy.data.worlds.new("Three.js World")
        world.use_nodes = True
        self.context.scene.world = world
        world["three_background_json"] = json.dumps(root_node.get("background"), ensure_ascii=False)
        world["three_environment_json"] = json.dumps(root_node.get("environment"), ensure_ascii=False)
        world["three_fog_json"] = json.dumps(root_node.get("fog"), ensure_ascii=False)
        world["three_scene_settings_json"] = json.dumps({
            key: root_node.get(key) for key in (
                "backgroundBlurriness", "backgroundIntensity", "backgroundRotation",
                "environmentIntensity", "environmentRotation",
            ) if key in root_node
        }, ensure_ascii=False)
        tree = world.node_tree
        tree.nodes.clear()
        output = tree.nodes.new("ShaderNodeOutputWorld")
        background = next((node for node in tree.nodes if node.type == "BACKGROUND"), None)
        if background is None:
            background = tree.nodes.new("ShaderNodeBackground")
        tree.links.new(background.outputs["Background"], output.inputs["Surface"])
        bg_value = root_node.get("background")
        user_data = root_node.get("userData") or {}
        renderer_settings = user_data.get("__threeBlenderRenderer", {}) if isinstance(user_data, dict) else {}
        if bg_value is None and isinstance(renderer_settings.get("clearColor"), (int, float)):
            bg_value = renderer_settings["clearColor"]
        if isinstance(bg_value, (int, float)):
            background.inputs["Color"].default_value = (*_hex_rgb(bg_value), 1.0)
        elif bg_value is None:
            background.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        background.inputs["Strength"].default_value = float(root_node.get("backgroundIntensity", 1.0))

        texture_uuid = bg_value if isinstance(bg_value, str) else root_node.get("environment")
        image = self.load_texture_image(texture_uuid) if texture_uuid else None
        if image:
            env = tree.nodes.new("ShaderNodeTexEnvironment")
            env.image = image
            tree.links.new(env.outputs["Color"], background.inputs["Color"])
            if texture_uuid == root_node.get("environment") and bg_value is None:
                background.inputs["Strength"].default_value = float(root_node.get("environmentIntensity", 1.0))

        self.context.scene["three_scene_settings_json"] = json.dumps({
            key: root_node.get(key) for key in ("fog", "background", "environment") if key in root_node
        }, ensure_ascii=False)
        if isinstance(renderer_settings, dict):
            self.context.scene["three_renderer_settings_json"] = json.dumps(renderer_settings, ensure_ascii=False)
            try:
                self.context.scene.view_settings.exposure = float(renderer_settings.get("toneMappingExposure", 1.0))
            except (TypeError, ValueError, AttributeError):
                pass
            if renderer_settings.get("toneMapping") == 4:
                try:
                    self.context.scene.view_settings.view_transform = "AgX"
                except (TypeError, ValueError, AttributeError):
                    pass
        active_camera_uuid = user_data.get("__threeBlenderActiveCamera") if isinstance(user_data, dict) else None
        active_camera = self.objects_by_uuid.get(active_camera_uuid)
        if active_camera and active_camera.type == "CAMERA":
            self.context.scene.camera = active_camera

    def get_material(self, uuid):
        if not self.import_materials or not uuid:
            return None
        if uuid in self.materials:
            return self.materials[uuid]
        src = self.material_json.get(uuid)
        if src is None:
            return None
        mat = _make_material(src)
        mat["three_material_json"] = json.dumps(src, ensure_ascii=False)
        bsdf = next((node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
        if bsdf:
            map_slots = (
                "map", "normalMap", "roughnessMap", "metalnessMap", "emissiveMap", "alphaMap",
                "aoMap", "clearcoatMap", "clearcoatRoughnessMap", "sheenColorMap",
                "sheenRoughnessMap", "specularMap", "specularColorMap", "specularIntensityMap",
                "iridescenceMap", "iridescenceThicknessMap", "transmissionMap", "thicknessMap",
            )
            for slot in map_slots:
                texture = self.texture_json.get(src.get(slot))
                image = self.image_json.get(texture.get("image")) if texture else None
                if texture and image:
                    _wire_texture(mat, bsdf, slot, texture, image, self.filepath)
        self.materials[uuid] = mat
        return mat

    def attach_materials(self, obj, node):
        ref = node.get("material")
        if not ref or obj.type != "MESH":
            return

        refs = ref if isinstance(ref, list) else [ref]
        attached = []
        for uuid in refs:
            mat = self.get_material(uuid)
            if mat is not None:
                obj.data.materials.append(mat)
                attached.append(mat)
                if self.material_json.get(uuid, {}).get("flatShading"):
                    for polygon in obj.data.polygons:
                        polygon.use_smooth = False
        wireframe_refs = [self.material_json.get(uuid, {}) for uuid in refs]
        if attached and wireframe_refs and all(material.get("wireframe") for material in wireframe_refs):
            bounds = [0.0, 0.0, 0.0]
            if obj.data.vertices:
                for axis in range(3):
                    coords = [vertex.co[axis] for vertex in obj.data.vertices]
                    bounds[axis] = max(coords) - min(coords)
            opacity = float(wireframe_refs[0].get("opacity", 1.0))
            width_factor = 0.00012 if opacity < 0.05 else 0.0035
            thickness = max(max(bounds, default=0.0) * width_factor, 0.001)
            modifier = obj.modifiers.new("Three.js Wireframe", "WIREFRAME")
            modifier.thickness = thickness
            modifier.use_replace = True
            modifier.use_boundary = True
            modifier.material_offset = 0
            obj["three_wireframe_thickness"] = thickness

    def create_node(self, node, collection, parent=None):
        node_type = node.get("type", "Object3D")
        name = node.get("name") or node_type or "ThreeObject"

        if node_type in {"Mesh", "SkinnedMesh", "Line", "LineLoop", "LineSegments", "Points"}:
            geo_ref = node.get("geometry")
            geo = self.geometry_json.get(geo_ref)
            if geo is not None:
                mesh = _build_mesh_datablock(
                    name=f"{name}_Mesh",
                    geometry=geo,
                    object_type=node_type,
                    convert_axes=self.convert_axes,
                    global_scale=self.global_scale,
                )
                obj = bpy.data.objects.new(name, mesh)
                self.attach_materials(obj, node)
                if node_type == "SkinnedMesh":
                    obj["three_import_warning"] = "SkinnedMesh imported as static mesh; rigging is not imported in v0.1."
            else:
                obj = bpy.data.objects.new(name, None)
                obj["three_import_warning"] = f"Geometry UUID not found: {geo_ref}"
        elif node_type in {"AmbientLight", "HemisphereLight", "DirectionalLight", "PointLight", "SpotLight", "RectAreaLight"}:
            light_type = {"DirectionalLight": "SUN", "PointLight": "POINT", "SpotLight": "SPOT", "RectAreaLight": "AREA", "AmbientLight": "AREA", "HemisphereLight": "AREA"}[node_type]
            light = bpy.data.lights.new(name, light_type)
            color = _hex_rgb(node.get("color", 0xFFFFFF))
            light.color = color
            light.energy = float(node.get("intensity", 1.0))
            light["three_light_type"] = node_type
            light["three_intensity"] = float(node.get("intensity", 1.0))
            if node_type == "HemisphereLight":
                light["three_ground_color"] = json.dumps(_hex_rgb(node.get("groundColor", 0x000000)))
            if light_type in {"POINT", "SPOT"}:
                light.cutoff_distance = max(float(node.get("distance", 0.0)), 0.0) or 25.0
                if hasattr(light, "use_custom_distance"):
                    light.use_custom_distance = float(node.get("distance", 0.0)) > 0.0
                if light_type == "SPOT":
                    light.spot_size = min(float(node.get("angle", math.pi / 3.0)) * 2.0, math.pi)
                    light.spot_blend = float(node.get("penumbra", 0.0))
            elif light_type == "AREA":
                light.shape = "RECTANGLE"
                light.size = float(node.get("width", 1.0))
                light.size_y = float(node.get("height", 1.0))
            obj = bpy.data.objects.new(name, light)
        elif node_type in {"PerspectiveCamera", "OrthographicCamera"}:
            camera = bpy.data.cameras.new(name)
            if node_type == "PerspectiveCamera":
                camera.lens = 36.0 / (2.0 * math.tan(math.radians(float(node.get("fov", 50.0))) / 2.0))
            else:
                camera.type = "ORTHO"
                camera.ortho_scale = float(node.get("right", 1.0)) - float(node.get("left", -1.0))
            camera.clip_start = float(node.get("near", 0.1))
            camera.clip_end = float(node.get("far", 2000.0))
            obj = bpy.data.objects.new(name, camera)
            self.context.scene.camera = obj
        else:
            obj = bpy.data.objects.new(name, None)

        collection.objects.link(obj)

        obj.parent = parent
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_local = _convert_matrix(
            _node_local_matrix(node),
            convert_axes=self.convert_axes,
            global_scale=self.global_scale,
        )

        obj.hide_viewport = not bool(node.get("visible", True))
        obj.hide_render = not bool(node.get("visible", True))
        if hasattr(obj, "show_relationship_lines"):
            obj.show_relationship_lines = False
        obj["three_type"] = node_type
        obj["three_uuid"] = node.get("uuid", "")
        obj["three_object_json"] = json.dumps(node, ensure_ascii=False)
        if node.get("uuid"):
            self.objects_by_uuid[node["uuid"]] = obj
        self.objects_by_name.setdefault(name, obj)

        if self.keep_user_data and "userData" in node:
            try:
                obj["three_user_data_json"] = json.dumps(node["userData"], ensure_ascii=False)
            except Exception:
                pass

        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                self.create_node(child, collection, obj)

        return obj

    def import_full_object_json(self):
        root_node = self.data.get("object")
        if not isinstance(root_node, dict):
            raise ValueError("No root 'object' found in Three.js Object3D JSON")

        collection_name = Path(self.filepath).stem
        collection = bpy.data.collections.new(collection_name)
        self.context.scene.collection.children.link(collection)
        root = self.create_node(root_node, collection, None)
        self.apply_scene_settings(root_node)
        self.import_animation_clips(root, root_node)
        _add_shader_bloom_approximation(self.context.scene, self.materials.values())
        return root

    def _animation_targets(self, node, clips):
        for animation_ref in node.get("animations", []) or []:
            clip = animation_ref if isinstance(animation_ref, dict) else self.animation_json.get(animation_ref)
            if isinstance(clip, dict):
                clips.setdefault(clip.get("uuid") or clip.get("name") or str(id(clip)), clip)
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                self._animation_targets(child, clips)

    def _animation_fcurve(self, action, obj, data_path, index, group_name):
        if hasattr(action, "fcurve_ensure_for_datablock"):
            return action.fcurve_ensure_for_datablock(
                obj, data_path, index=index, group_name=group_name
            )
        return action.fcurves.new(data_path, index=index, action_group=group_name)

    def import_animation_clips(self, root, root_node):
        clips = {}
        self._animation_targets(root_node, clips)
        user_data = root_node.get("userData") or {}
        history = user_data.get("__threeBlenderTransformSamples") if isinstance(user_data, dict) else None
        samples = history.get("samples", []) if isinstance(history, dict) else []
        root["three_animation_sample_count"] = len(samples)
        imported_track_count = 0
        imported_action_count = 0
        if len(samples) >= 2:
            start_time = float(samples[0].get("time", 0.0))
            times = [float(sample.get("time", start_time)) - start_time for sample in samples]
            uuids = set().union(*(sample.get("objects", {}).keys() for sample in samples))
            tracks = []
            for uuid in uuids:
                for prop, size in (("position", 3), ("rotation", 3), ("scale", 3)):
                    values = []
                    track_times = []
                    for time_value, sample in zip(times, samples):
                        transform = sample.get("objects", {}).get(uuid)
                        value = transform.get(prop) if isinstance(transform, dict) else None
                        if isinstance(value, list) and len(value) >= size:
                            track_times.append(time_value)
                            values.extend(value[:size])
                    if len(track_times) >= 2 and any(
                        abs(float(values[index]) - float(values[index % size])) > 1e-5
                        for index in range(size, len(values))
                    ):
                        tracks.append({"name": f"{uuid}.{prop}", "times": track_times, "values": values})
            if tracks:
                clips["__threeBlenderBakedRuntime"] = {
                    "name": "Three.js Runtime Transform Capture",
                    "duration": max(times),
                    "tracks": tracks,
                }
        if not clips:
            root["three_animation_track_count"] = 0
            root["three_animation_action_count"] = 0
            root["three_animation_key_count"] = 0
            return
        fps = self.context.scene.render.fps / max(self.context.scene.render.fps_base, 0.001)
        frame_start = None
        frame_end = None
        imported_key_count = 0
        for clip in clips.values():
            is_runtime_capture = clip.get("name") == "Three.js Runtime Transform Capture"
            tracks_by_object = {}
            for track in clip.get("tracks", []) or []:
                track_name = str(track.get("name", ""))
                target_name, _, property_name = track_name.rpartition(".")
                target = self.objects_by_uuid.get(target_name) or self.objects_by_name.get(target_name)
                if target is None and not target_name:
                    target = root
                if target is None:
                    continue
                match = re.fullmatch(r"(position|quaternion|rotation|scale)(?:\[([xyzw])\])?", property_name)
                if not match:
                    continue
                prop, component = match.groups()
                times = track.get("times", [])
                values = track.get("values", [])
                if not times:
                    continue
                value_components = 1 if component else (4 if prop == "quaternion" else 3)
                components = value_components
                if len(values) < len(times) * value_components:
                    continue
                runtime_rotation = is_runtime_capture and prop == "rotation"
                euler_rotation = prop == "rotation" and (runtime_rotation or component is not None)
                data_path = "rotation_euler" if euler_rotation else "rotation_quaternion" if prop in {"quaternion", "rotation"} else "location" if prop == "position" else "scale"
                target_index = None
                sign = 1.0
                if component:
                    source_index = "xyzw".index(component)
                    if prop in {"position", "scale", "rotation"} and source_index < 3:
                        if self.convert_axes:
                            axis_map = (0, 2, 1)
                            target_index = axis_map[source_index]
                            if prop in {"position", "rotation"} and source_index == 2:
                                sign = -1.0
                        else:
                            target_index = source_index
                    elif prop == "quaternion":
                        continue
                    else:
                        continue
                    components = 1
                elif prop == "rotation":
                    components = 3 if runtime_rotation else 4
                    target.rotation_mode = "XYZ" if euler_rotation else "QUATERNION"
                elif prop == "quaternion":
                    target.rotation_mode = "QUATERNION"

                curves = tracks_by_object.setdefault(target, {})
                if target_index is not None:
                    curves.setdefault((data_path, target_index), [])
                else:
                    for axis in range(components):
                        curves.setdefault((data_path, axis), [])

                previous_euler = None
                for key_index, seconds in enumerate(times):
                    frame = self.context.scene.frame_start + float(seconds) * fps
                    value_offset = key_index * value_components
                    raw_values = values[value_offset:value_offset + value_components]
                    if component:
                        key_values = [(target_index, float(raw_values[0]) * sign)]
                    elif prop == "position":
                        vec = _convert_vec3(raw_values, self.convert_axes, self.global_scale)
                        key_values = list(enumerate(vec))
                    elif prop == "scale":
                        scale = (raw_values[0], raw_values[2], raw_values[1]) if self.convert_axes else raw_values
                        key_values = list(enumerate(scale))
                    elif prop == "quaternion":
                        q = Quaternion((raw_values[3], raw_values[0], raw_values[1], raw_values[2]))
                        if self.convert_axes:
                            q = _convert_matrix(q.to_matrix().to_4x4(), True).to_quaternion()
                        key_values = list(enumerate(tuple(q)))
                    else:
                        order = "XYZ"
                        source_node = self.objects_by_uuid.get(target.get("three_uuid"))
                        if source_node:
                            try:
                                source_data = json.loads(source_node.get("three_object_json", "{}"))
                                order = source_data.get("rotationOrder", order)
                            except Exception:
                                pass
                        q = Euler(tuple(raw_values[:3]), order).to_quaternion()
                        if self.convert_axes:
                            q = _convert_matrix(q.to_matrix().to_4x4(), True).to_quaternion()
                        if runtime_rotation:
                            angles = q.to_euler("XYZ") if previous_euler is None else q.to_euler("XYZ", previous_euler)
                            previous_euler = angles.copy()
                            key_values = list(enumerate(tuple(angles)))
                        else:
                            key_values = list(enumerate(tuple(q)))
                    frame_start = frame if frame_start is None else min(frame_start, frame)
                    frame_end = frame if frame_end is None else max(frame_end, frame)
                    for axis, value in key_values:
                        curves.setdefault((data_path, axis), []).append((frame, float(value)))

            for obj, curves in tracks_by_object.items():
                if not curves:
                    continue
                action_name = f"{clip.get('name') or 'Three.js Animation'} | {obj.name}"
                action = bpy.data.actions.new(action_name)
                imported_action_count += 1
                imported_track_count += len(curves)
                anim_data = obj.animation_data_create()
                previous_action = anim_data.action
                anim_data.action = action
                for (data_path, axis), keys in curves.items():
                    try:
                        imported_key_count += len(keys)
                        prop = getattr(obj, data_path)
                        for frame, value in keys:
                            prop[axis] = value
                            obj.keyframe_insert(
                                data_path=data_path,
                                index=axis,
                                frame=frame,
                                group=clip.get("name") or "Three.js",
                            )
                        curve = self._animation_fcurve(action, obj, data_path, axis, clip.get("name") or "Three.js")
                        for point in curve.keyframe_points:
                            point.interpolation = "LINEAR"
                        curve.update()
                        cycle = curve.modifiers.new("CYCLES")
                        if runtime_rotation and data_path == "rotation_euler":
                            deltas = [right[1] - left[1] for left, right in zip(keys, keys[1:])]
                            mean_delta = sum(deltas) / max(len(deltas), 1)
                            stable_spin = abs(mean_delta) > 1e-4 and max(
                                (abs(delta - mean_delta) for delta in deltas), default=0.0
                            ) < abs(mean_delta) * 0.25
                            if stable_spin:
                                cycle.mode_before = "REPEAT_OFFSET"
                                cycle.mode_after = "REPEAT_OFFSET"
                    except Exception as exc:
                        obj["three_animation_import_warning"] = str(exc)[:240]
                if previous_action is not None:
                    anim_data.action = previous_action
                    nla_track = anim_data.nla_tracks.new()
                    nla_track.name = action_name
                    start = min((key[0] for keys in curves.values() for key in keys), default=1.0)
                    nla_strip = nla_track.strips.new(action_name, start, action)
                    if hasattr(action, "slots") and len(action.slots):
                        try:
                            nla_strip.action_slot = action.slots[0]
                        except Exception:
                            pass
        root["three_animation_track_count"] = imported_track_count
        root["three_animation_action_count"] = imported_action_count
        root["three_animation_key_count"] = imported_key_count
        if frame_start is not None and frame_end is not None:
            self.context.scene.frame_start = int(math.floor(frame_start))
            self.context.scene.frame_end = max(int(math.ceil(frame_end)), self.context.scene.frame_start + 1)

    def import_bare_geometry_json(self):
        collection_name = Path(self.filepath).stem
        collection = bpy.data.collections.new(collection_name)
        self.context.scene.collection.children.link(collection)

        mesh = _build_mesh_datablock(
            name=collection_name,
            geometry=self.data,
            object_type="Mesh",
            convert_axes=self.convert_axes,
            global_scale=self.global_scale,
        )
        obj = bpy.data.objects.new(collection_name, mesh)
        collection.objects.link(obj)
        return obj

    def run(self):
        if isinstance(self.data, dict) and isinstance(self.data.get("object"), dict):
            return self.import_full_object_json()

        if _geometry_payload(self.data) is not None:
            return self.import_bare_geometry_json()

        raise ValueError(
            "Unsupported JSON. Expected Three.js Object3D/Scene.toJSON() "
            "or BufferGeometry.toJSON()."
        )


def _runtime_http_handler(token):
    class RuntimeSceneHandler(BaseHTTPRequestHandler):
        def _cors(self):
            origin = self.headers.get("Origin", "*")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Three-Scene-Name")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _reply(self, status, body=b""):
            self.send_response(status)
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            if body:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_OPTIONS(self):
            self._reply(204)

        def do_POST(self):
            global _RUNTIME_LAST_ERROR, _RUNTIME_LAST_STATUS
            if self.path not in {"/scene", "/status"}:
                self._reply(404, b'{"error":"Use POST /scene"}')
                return
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                self._reply(401, b'{"error":"Invalid receiver token"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > _RUNTIME_MAX_BYTES:
                self._reply(413, b'{"error":"Scene payload must be 1 byte to 128 MiB"}')
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/status":
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object")
                    _RUNTIME_LAST_ERROR = str(data.get("error", ""))[:500]
                    _RUNTIME_LAST_STATUS = str(data.get("status", ""))[:160]
                    self._reply(202, b'{"status":"recorded"}')
                    return
                if not isinstance(data, dict) or not (
                    isinstance(data.get("object"), dict) or _geometry_payload(data) is not None
                ):
                    raise ValueError("Expected Three.js Object3D/Scene JSON or BufferGeometry JSON")
            except Exception as exc:
                response = json.dumps({"error": str(exc)}).encode("utf-8")
                self._reply(400, response)
                return
            scene_name = self.headers.get("X-Three-Scene-Name", "Live Three.js Scene")
            try:
                while True:
                    _RUNTIME_QUEUE.get_nowait()
            except queue.Empty:
                pass
            _RUNTIME_QUEUE.put((scene_name[:120], data))
            _RUNTIME_LAST_ERROR = ""
            _RUNTIME_LAST_STATUS = "Scene received from browser"
            self._reply(202, b'{"status":"queued"}')

        def log_message(self, _format, *_args):
            pass

    return RuntimeSceneHandler


def _import_queued_runtime_scene():
    global _RUNTIME_LAST_IMPORT_REPORT
    latest = None
    while True:
        try:
            latest = _RUNTIME_QUEUE.get_nowait()
        except queue.Empty:
            break
    if latest is None:
        raise RuntimeError("No captured scene is ready to import")
    scene_name, data = latest
    old_collection = bpy.data.collections.get(_RUNTIME_PREVIEW_COLLECTION)
    if old_collection is not None:
        for obj in list(old_collection.objects):
            data_block = obj.data if obj.type in {"MESH", "CURVE", "LIGHT", "CAMERA"} else None
            old_materials = list(data_block.materials) if obj.type == "MESH" else []
            bpy.data.objects.remove(obj, do_unlink=True)
            if data_block is not None and data_block.users == 0:
                if data_block.bl_rna.identifier == "Mesh":
                    bpy.data.meshes.remove(data_block)
                elif data_block.bl_rna.identifier == "Curve":
                    bpy.data.curves.remove(data_block)
                elif data_block.bl_rna.identifier == "Light":
                    bpy.data.lights.remove(data_block)
                elif data_block.bl_rna.identifier == "Camera":
                    bpy.data.cameras.remove(data_block)
            for material in old_materials:
                if material.users == 0 and material.get("three_uuid"):
                    bpy.data.materials.remove(material)
        bpy.data.collections.remove(old_collection)
    importer = ThreeJSONImportContext(
        context=bpy.context,
        json_data=data,
        filepath=f"{_RUNTIME_PREVIEW_COLLECTION}.three.json",
    )
    root = importer.run()
    if root is not None:
        root["three_runtime_source"] = scene_name
        sample_count = int(root.get("three_animation_sample_count", 0))
        track_count = int(root.get("three_animation_track_count", 0))
        key_count = int(root.get("three_animation_key_count", 0))
        action_count = int(root.get("three_animation_action_count", 0))
        if key_count:
            _RUNTIME_LAST_IMPORT_REPORT = (
                f"Imported animation: {track_count} curves, {key_count} keys, "
                f"{sample_count} samples across {action_count} actions"
            )
        elif sample_count:
            _RUNTIME_LAST_IMPORT_REPORT = (
                f"Captured {sample_count} runtime samples, but no changing transforms were found"
            )
        else:
            _RUNTIME_LAST_IMPORT_REPORT = "No runtime animation samples in this capture"
        root.select_set(True)
        bpy.context.view_layer.objects.active = root
        for collection in root.users_collection:
            collection.name = _RUNTIME_PREVIEW_COLLECTION
    # Imported scene objects are the focus; Blender's relation lines and floor
    # can otherwise obscure thin Three.js wireframes and orbit paths.
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            overlay = area.spaces.active.overlay
            for setting in ("show_relationship_lines", "show_extras", "show_floor", "show_axis_x", "show_axis_y"):
                if hasattr(overlay, setting):
                    setattr(overlay, setting, False)
    print(f"[Three.js Importer] Imported runtime scene: {scene_name}")
    return scene_name


def _runtime_ui_refresh_timer():
    if _RUNTIME_SERVER is None:
        return None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 0.5


def _start_runtime_server(port):
    global _RUNTIME_SERVER, _RUNTIME_TOKEN, _RUNTIME_PORT, _RUNTIME_LAST_ERROR, _RUNTIME_LAST_STATUS
    if _RUNTIME_SERVER is not None:
        raise RuntimeError("The runtime receiver is already running")
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", port), _runtime_http_handler(token))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="ThreeJSReceiver", daemon=True)
    thread.start()
    _RUNTIME_SERVER = server
    _RUNTIME_TOKEN = token
    _RUNTIME_PORT = port
    _RUNTIME_LAST_ERROR = ""
    _RUNTIME_LAST_STATUS = ""
    if not bpy.app.timers.is_registered(_runtime_ui_refresh_timer):
        bpy.app.timers.register(_runtime_ui_refresh_timer, first_interval=0.5)
    return token


def _validate_runtime_port(window_manager):
    preview_url = window_manager.threejs_preview_url.strip()
    try:
        parsed = urllib.parse.urlparse(preview_url)
        preview_port = parsed.port
    except ValueError:
        preview_port = None
    if preview_port and preview_port == window_manager.threejs_runtime_port:
        raise RuntimeError(
            f"The preview server uses port {preview_port}. Set Receiver Port to a different port, such as 8765."
        )


def _stop_runtime_server():
    global _RUNTIME_SERVER, _RUNTIME_TOKEN, _RUNTIME_PORT, _RUNTIME_LAST_ERROR, _RUNTIME_LAST_STATUS
    server = _RUNTIME_SERVER
    _RUNTIME_SERVER = None
    _RUNTIME_TOKEN = ""
    _RUNTIME_PORT = 0
    _RUNTIME_LAST_ERROR = ""
    _RUNTIME_LAST_STATUS = ""
    if server is not None:
        server.shutdown()
        server.server_close()
    if bpy.app.timers.is_registered(_runtime_ui_refresh_timer):
        bpy.app.timers.unregister(_runtime_ui_refresh_timer)


def _project_preview_command(project_dir, command_override):
    if command_override.strip():
        return command_override.strip(), ""
    package_json = os.path.join(project_dir, "package.json")
    if os.path.isfile(package_json):
        with open(package_json, "r", encoding="utf-8") as package_file:
            package = json.load(package_file)
        scripts = package.get("scripts", {})
        dev_script = scripts.get("dev", "").lower()
        package_manager = (
            "pnpm" if os.path.isfile(os.path.join(project_dir, "pnpm-lock.yaml")) else
            "yarn" if os.path.isfile(os.path.join(project_dir, "yarn.lock")) else
            "bun" if os.path.isfile(os.path.join(project_dir, "bun.lock")) or os.path.isfile(os.path.join(project_dir, "bun.lockb")) else
            "npm"
        )
        if "dev" in scripts:
            host_flag = "--hostname" if "next dev" in dev_script else "--host"
            if package_manager == "yarn":
                command = f"yarn dev {host_flag} 127.0.0.1"
            else:
                runner = {"npm": "npm run", "pnpm": "pnpm run", "bun": "bun run"}[package_manager]
                command = f"{runner} dev -- {host_flag} 127.0.0.1"
            if "next dev" in dev_script or "nuxt dev" in dev_script or "react-scripts start" in dev_script:
                url = "http://127.0.0.1:3000"
            elif "ng serve" in dev_script:
                url = "http://127.0.0.1:4200"
            elif "astro dev" in dev_script:
                url = "http://127.0.0.1:4321"
            else:
                url = "http://127.0.0.1:5173"
            return command, url
        if "start" in scripts:
            runner = {"npm": "npm start", "pnpm": "pnpm start", "yarn": "yarn start", "bun": "bun run start"}[package_manager]
            if "ng serve" in scripts["start"].lower():
                return runner, "http://127.0.0.1:4200"
            return runner, "http://127.0.0.1:3000"
    if os.path.isfile(os.path.join(project_dir, "index.html")):
        return f'"{sys.executable}" -m http.server 5173 --bind 127.0.0.1', "http://127.0.0.1:5173"
    if os.path.isfile(package_json):
        raise RuntimeError("package.json has no 'dev' or 'start' script; enter a custom preview command")
    raise RuntimeError("No package.json or index.html found. Enter a custom preview command.")


def _is_vite_project(project_dir):
    package_json = os.path.join(project_dir, "package.json")
    if not os.path.isfile(package_json):
        return False
    with open(package_json, "r", encoding="utf-8") as package_file:
        package = json.load(package_file)
    dev_script = package.get("scripts", {}).get("dev", "").lower()
    framework_cli = ("next ", "nuxt ", "astro ", "ng ", "react-scripts ", "vue-cli-service ")
    if any(cli in dev_script for cli in framework_cli):
        return False
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    has_vite_config = any(
        os.path.isfile(os.path.join(project_dir, filename))
        for filename in ("vite.config.js", "vite.config.mjs", "vite.config.ts", "vite.config.mts")
    )
    return "vite" in dev_script or ("vite" in dependencies and has_vite_config)


def _install_vite_runtime_capture(project_dir, receiver_port, preview_port, token):
    """Generate an ephemeral Vite config that hooks the runtime renderer."""
    global _PREVIEW_GENERATED_FILES
    helper_dir = os.path.join(project_dir, "src") if os.path.isdir(os.path.join(project_dir, "src")) else project_dir
    helper_name = f"three_export_helper_blender_{receiver_port}_{token[:8]}.js"
    helper_path = os.path.join(helper_dir, helper_name)
    helper_source = os.path.join(os.path.dirname(__file__), "three_export_helper.js")
    with open(helper_source, "rb") as helper_file:
        helper_contents = helper_file.read()
    if os.path.exists(helper_path):
        with open(helper_path, "rb") as existing_file:
            if existing_file.read() != helper_contents:
                raise RuntimeError(f"Bridge file already exists with different contents: {helper_path}")
    else:
        with open(helper_path, "wb") as helper_file:
            helper_file.write(helper_contents)
        _PREVIEW_GENERATED_FILES.append((helper_path, helper_contents))

    bridge_name = f".threejs_blender_vite_bridge_{receiver_port}_{token[:8]}.mjs"
    bridge_path = os.path.join(project_dir, bridge_name)
    helper_url = "/" + os.path.relpath(helper_path, project_dir).replace(os.sep, "/")
    token_js = json.dumps(token)
    helper_url_js = json.dumps(helper_url)
    bridge_source = f'''import {{ createServer, loadConfigFromFile }} from "vite";
const root = process.cwd();
const loaded = await loadConfigFromFile({{ command: "serve", mode: "development" }}, undefined, root);
const token = {token_js};
const helperUrl = {helper_url_js};
const capturePlugin = {{
  name: "threejs-blender-live-capture",
  transformIndexHtml: {{
    order: "pre",
    handler(html) {{
      const script = `<script type="module">
        import * as THREE from "three";
        import {{ startThreeSceneSync }} from "${{helperUrl}}";
        const token = ${{JSON.stringify(token)}};
        const reportStatus = (status, error = "") => fetch("http://127.0.0.1:{receiver_port}/status", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", Authorization: "Bearer " + token }},
          body: JSON.stringify({{ status, error }}),
        }}).catch(() => {{}});
        void reportStatus("Vite bridge loaded; waiting for a Three.js render");
        const reportCaptureError = (error) => {{
          console.error("Three.js to Blender capture failed", error);
          void reportStatus("Scene capture failed", String(error));
        }};
        let latestScene = null;
        let latestCamera = null;
        let latestRenderer = null;
        let candidateScene = null;
        let candidateCamera = null;
        let candidateRenderer = null;
        let candidateScore = -Infinity;
        let frameCommitScheduled = false;
        let stopSync = null;
        const rendererPrototype = THREE.WebGLRenderer.prototype;
        const renderDescriptor = Object.getOwnPropertyDescriptor(rendererPrototype, "render");
        const scoreScene = (scene, camera) => {{
          let objects = 0;
          let meshes = 0;
          let lights = 0;
          let shaderMeshes = 0;
          let vertices = 0;
          scene.traverse((object) => {{
            objects += 1;
            if (object.isLight) lights += 1;
            if (object.isMesh) {{
              meshes += 1;
              const materials = Array.isArray(object.material) ? object.material : [object.material];
              if (materials.length && materials.every((material) => material?.isShaderMaterial)) shaderMeshes += 1;
              vertices += object.geometry?.attributes?.position?.count || 0;
            }}
          }});
          let score = objects * 3 + meshes * 100 + lights * 80 + Math.sqrt(vertices);
          if (camera?.isPerspectiveCamera) score += 10000;
          if (camera?.isOrthographicCamera) score -= 100;
          if (camera?.isOrthographicCamera && meshes <= 1 && shaderMeshes === meshes && lights === 0) score -= 100000;
          return score;
        }};
        const captureScene = (scene, camera, renderer) => {{
          const score = scoreScene(scene, camera);
          if (score > candidateScore) {{
            candidateScene = scene;
            candidateCamera = camera;
            candidateRenderer = renderer;
            candidateScore = score;
          }}
          if (frameCommitScheduled) return;
          frameCommitScheduled = true;
          requestAnimationFrame(() => {{
            frameCommitScheduled = false;
            if (candidateScene) {{
              latestScene = candidateScene;
              latestCamera = candidateCamera;
              latestRenderer = candidateRenderer;
            }}
            candidateScene = null;
            candidateCamera = null;
            candidateRenderer = null;
            candidateScore = -Infinity;
            if (latestScene && !stopSync) {{
              void reportStatus("Main Three.js scene found; sending scene");
              stopSync = startThreeSceneSync(() => latestScene, THREE, {{
                url: "http://127.0.0.1:{receiver_port}/scene",
                token,
                intervalMs: 1500,
                getActiveCamera: () => latestCamera,
                getRenderer: () => latestRenderer,
                onAnimationHistory: (count) => reportStatus(
                  count >= 240 ? "Scene ready; captured 24 seconds of looping transform animation" :
                    "Scene captured; building animation history " + Math.round(count / 10) + " / 24 seconds"
                ),
                onError: reportCaptureError,
              }});
            }}
          }});
        }};
        const wrapRender = (render) => function(scene, camera, ...args) {{
          captureScene(scene, camera, this);
          return render.call(this, scene, camera, ...args);
        }};
        if (renderDescriptor && typeof renderDescriptor.value === "function") {{
          Object.defineProperty(rendererPrototype, "render", {{
            ...renderDescriptor,
            value: wrapRender(renderDescriptor.value),
          }});
        }} else {{
          const renderSlot = Symbol.for("threejs-blender-runtime-render");
          Object.defineProperty(rendererPrototype, "render", {{
            configurable: true,
            get() {{ return this[renderSlot]; }},
            set(render) {{ this[renderSlot] = wrapRender(render); }},
          }});
        }}
        window.addEventListener("beforeunload", () => stopSync?.(), {{ once: true }});
      </script>`;
      return html.replace("</head>", `${{script}}</head>`);
    }},
  }},
}};
const userConfig = loaded?.config || {{}};
const config = {{
  ...userConfig,
  configFile: false,
  root,
  plugins: [capturePlugin, ...(userConfig.plugins || [])],
  server: {{ ...(userConfig.server || {{}}), host: "127.0.0.1", port: {preview_port}, strictPort: true }},
}};
const server = await createServer(config);
await server.listen();
server.printUrls();
'''
    if os.path.exists(bridge_path):
        raise RuntimeError(f"Generated bridge config already exists: {bridge_path}")
    with open(bridge_path, "w", encoding="utf-8") as bridge_file:
        bridge_file.write(bridge_source)
    _PREVIEW_GENERATED_FILES.append((bridge_path, bridge_source.encode("utf-8")))
    return f'node "{bridge_name}"'


def _next_project_info(project_dir):
    package_path = os.path.join(project_dir, "package.json")
    if not os.path.isfile(package_path):
        return None
    try:
        with open(package_path, "r", encoding="utf-8") as package_file:
            package = json.load(package_file)
    except (OSError, ValueError):
        return None
    scripts = package.get("scripts", {})
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if "next" not in dependencies or "next" not in str(scripts.get("dev", "")).lower():
        return None
    version = str(dependencies.get("next", ""))
    match = re.search(r"(\d+)\.(\d+)", version)
    if match and tuple(map(int, match.groups())) < (15, 3):
        return {"supported": False, "version": version}
    return {"supported": True, "version": version}


def _write_preview_file(path, contents):
    encoded = contents.encode("utf-8")
    if os.path.exists(path):
        raise RuntimeError(f"Generated capture file already exists: {path}")
    with open(path, "wb") as capture_file:
        capture_file.write(encoded)
    _PREVIEW_GENERATED_FILES.append((path, encoded))


def _patch_preview_import(path, import_line):
    """Add a temporary side-effect import, restoring the original source on stop."""
    with open(path, "rb") as source_file:
        original = source_file.read()
    text = original.decode("utf-8")
    if import_line in text:
        return
    updated = original + ("\n" if not text.endswith("\n") else "").encode("utf-8") + import_line.encode("utf-8") + b"\n"
    with open(path, "wb") as source_file:
        source_file.write(updated)
    _PREVIEW_MODIFIED_FILES.append((path, original, updated))


def _iter_react_source_files(project_dir):
    ignored = {"node_modules", ".next", ".git", "dist", "build", "coverage"}
    for current, dirs, files in os.walk(project_dir):
        dirs[:] = [name for name in dirs if name not in ignored]
        for filename in files:
            if os.path.splitext(filename)[1].lower() in {".js", ".jsx", ".ts", ".tsx"}:
                yield os.path.join(current, filename)


def _jsx_opening_tag_end(source, start):
    """Find a JSX opening tag's closing > while ignoring braces and quoted props."""
    index = start
    brace_depth = 0
    quote = None
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == ">" and brace_depth == 0:
            return index
        index += 1
    return -1


def _inject_r3f_capture(project_dir, capture_path, token_id):
    component_name = "ThreeBlenderCapture_" + re.sub(r"[^A-Za-z0-9_]", "_", token_id)
    jsx_marker = f"THREEJS_BLENDER_CAPTURE_{token_id}"
    import_marker = f"THREEJS_BLENDER_CAPTURE_IMPORT_{token_id}"
    changed = 0
    for source_path in _iter_react_source_files(project_dir):
        try:
            with open(source_path, "rb") as source_file:
                original_bytes = source_file.read()
            source = original_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not re.search(r"import\s+[\w\s{},*]+\s+from\s+['\"]@react-three/fiber['\"]", source):
            continue
        open_tags = []
        for match in re.finditer(r"<Canvas(?=[\s/>])", source):
            end = _jsx_opening_tag_end(source, match.end())
            if end < 0 or source[max(match.end(), end - 1):end].rstrip().endswith("/"):
                continue
            if jsx_marker not in source:
                line_start = source.rfind("\n", 0, match.start()) + 1
                indentation = re.match(r"\s*", source[line_start:match.start()]).group(0)
                insertion = f"\n{indentation}  {{/* {jsx_marker} */}}<{component_name} />"
                open_tags.append((end + 1, insertion))
        if not open_tags:
            continue
        for position, insertion in reversed(open_tags):
            source = source[:position] + insertion + source[position:]
        relative_capture = os.path.relpath(capture_path, os.path.dirname(source_path)).replace(os.sep, "/")
        if not relative_capture.startswith("."):
            relative_capture = "./" + relative_capture
        import_line = f'import {{ {component_name} }} from "{relative_capture}"; // {import_marker}'
        source = source.rstrip() + "\n" + import_line + "\n"
        updated_bytes = source.encode("utf-8")
        with open(source_path, "wb") as source_file:
            source_file.write(updated_bytes)
        _PREVIEW_MODIFIED_FILES.append((source_path, original_bytes, updated_bytes))
        changed += len(open_tags)
    return changed


_R3F_CAPTURE_SOURCE = r'''import { useEffect } from "react";
import { useThree } from "@react-three/fiber";
import * as THREE from "three";
import { startThreeSceneSync } from __HELPER_IMPORT__;

const receiverPort = __RECEIVER_PORT__;
const token = __TOKEN__;
const canvases = new Map();
let stopSync = null;
const reportStatus = (status, error = "") => fetch(`http://127.0.0.1:${receiverPort}/status`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ status, error }),
}).catch(() => {});
const score = (scene) => {
  let meshes = 0, lights = 0, vertices = 0;
  scene.traverse((object) => {
    if (object.isLight) lights += 1;
    if (object.isMesh) {
      meshes += 1;
      vertices += object.geometry?.attributes?.position?.count || 0;
    }
  });
  return meshes * 100 + lights * 80 + Math.sqrt(vertices);
};
const selectedCanvas = () => {
  let selected = null, highest = -Infinity;
  for (const canvas of canvases.values()) {
    const value = score(canvas.scene);
    if (value > highest) { selected = canvas; highest = value; }
  }
  return selected;
};
function registerCanvas(state) {
  const key = Symbol("threejs-blender-canvas");
  canvases.set(key, state);
  if (!stopSync) {
    void reportStatus("React Three Fiber Canvas found; sending live scene");
    stopSync = startThreeSceneSync(() => selectedCanvas()?.scene, THREE, {
      url: `http://127.0.0.1:${receiverPort}/scene`,
      token,
      intervalMs: 1500,
      getActiveCamera: () => selectedCanvas()?.camera,
      getRenderer: () => selectedCanvas()?.gl,
      onAnimationHistory: (count) => reportStatus(
        count >= 240 ? "Scene ready; captured 24 seconds of transform animation" :
          `Scene found; capturing animation ${Math.round(count / 10)} / 24 seconds`
      ),
      onError: (error) => {
        console.error("Three.js to Blender capture failed", error);
        void reportStatus("Scene capture failed", String(error));
      },
    });
  }
  return () => {
    canvases.delete(key);
    if (canvases.size === 0 && stopSync) {
      stopSync();
      stopSync = null;
    }
  };
}
export function __COMPONENT_NAME__() {
  const state = useThree();
  useEffect(() => registerCanvas(state), [state.scene, state.camera, state.gl]);
  return null;
}
void reportStatus("React Three Fiber capture adapter loaded; waiting for Canvas");
'''


def _remove_stale_react_capture(project_dir):
    """Remove orphaned temporary React/Next capture imports from older runs."""
    roots = [project_dir, os.path.join(project_dir, "src")]
    generated_entries = set()
    capture_pattern = re.compile(r"\.threejs_blender_capture_(\d+)_([A-Za-z0-9_-]+)\.js$")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for filename in os.listdir(root):
            match = capture_pattern.fullmatch(filename)
            if not match:
                continue
            generated_entries.add(filename)
            receiver_port, token_id = match.groups()
            helper_name = f"three_export_helper_blender_{receiver_port}_{token_id}.js"
            for stale_name in (filename, helper_name):
                stale_path = os.path.join(root, stale_name)
                try:
                    os.remove(stale_path)
                except OSError:
                    pass

    if not generated_entries:
        return
    import_pattern = re.compile(
        r"^\s*import\s+['\"]\./?(\.threejs_blender_capture_[A-Za-z0-9_-]+\.js)['\"]\s*;?\s*$"
    )
    for root in roots:
        if not os.path.isdir(root):
            continue
        for filename in ("instrumentation-client.js", "instrumentation-client.ts", "index.js", "index.jsx", "index.ts", "index.tsx"):
            source_path = os.path.join(root, filename)
            if not os.path.isfile(source_path):
                continue
            try:
                with open(source_path, "r", encoding="utf-8") as source_file:
                    lines = source_file.readlines()
                kept_lines = [
                    line for line in lines
                    if not (match := import_pattern.match(line)) or match.group(1) not in generated_entries
                ]
                if kept_lines != lines:
                    cleaned = "".join(kept_lines)
                    if not cleaned.strip() and filename.startswith("instrumentation-client"):
                        os.remove(source_path)
                    else:
                        with open(source_path, "w", encoding="utf-8", newline="") as source_file:
                            source_file.write(cleaned)
            except OSError:
                pass

    # Clean source injections made by earlier capture-adapter versions. Keep
    # unrelated edits and remove only lines/elements bearing our marker.
    import_marker = re.compile(rb"(?m)^\s*import\s+.*//\s*THREEJS_BLENDER_CAPTURE_IMPORT_[A-Za-z0-9_-]+\s*(?:\r?\n|$)")
    jsx_marker = re.compile(rb"\{\s*/\*\s*THREEJS_BLENDER_CAPTURE_[A-Za-z0-9_-]+\s*\*/\s*\}\s*<ThreeBlenderCapture_[A-Za-z0-9_]+\s*/>")
    for source_path in _iter_react_source_files(project_dir):
        try:
            with open(source_path, "rb") as source_file:
                contents = source_file.read()
            cleaned = jsx_marker.sub(b"", import_marker.sub(b"", contents))
            if cleaned != contents:
                with open(source_path, "wb") as source_file:
                    source_file.write(cleaned)
        except OSError:
            pass


_REACT_CAPTURE_SOURCE = r'''import * as THREE from "three";
import { startThreeSceneSync } from __HELPER_IMPORT__;

const receiverPort = __RECEIVER_PORT__;
const token = __TOKEN__;
const prototype = THREE.WebGLRenderer?.prototype;
const captureMarker = Symbol.for("threejs-blender-runtime-capture");
const reportStatus = (status, error = "") => fetch(`http://127.0.0.1:${receiverPort}/status`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ status, error }),
}).catch(() => {});

if (prototype && typeof prototype.render === "function" && !prototype[captureMarker]) {
  const originalRender = prototype.render;
  let candidate = null;
  let candidateScore = -Infinity;
  let latest = null;
  let sync = null;
  let commitPending = false;
  const score = (scene, camera) => {
    let objects = 0, meshes = 0, lights = 0, vertices = 0;
    scene.traverse((object) => {
      objects += 1;
      if (object.isLight) lights += 1;
      if (object.isMesh) {
        meshes += 1;
        vertices += object.geometry?.attributes?.position?.count || 0;
      }
    });
    return objects * 3 + meshes * 100 + lights * 80 + Math.sqrt(vertices)
      + (camera?.isPerspectiveCamera ? 10000 : 0)
      - (camera?.isOrthographicCamera ? 100 : 0);
  };
  const wrappedRender = function(scene, camera, ...args) {
    if (scene?.isObject3D && camera?.isCamera) {
      const nextScore = score(scene, camera);
      if (nextScore > candidateScore) {
        candidate = { scene, camera, renderer: this };
        candidateScore = nextScore;
      }
      if (!commitPending) {
        commitPending = true;
        requestAnimationFrame(() => {
          commitPending = false;
          if (candidate && (!latest || candidateScore > latest.score)) {
            latest = { ...candidate, score: candidateScore };
          }
          candidate = null;
          candidateScore = -Infinity;
          if (latest && !sync) {
            void reportStatus("Three.js renderer found; sending scene to Blender");
            sync = startThreeSceneSync(() => latest.scene, THREE, {
              url: `http://127.0.0.1:${receiverPort}/scene`,
              token,
              intervalMs: 1500,
              getActiveCamera: () => latest.camera,
              getRenderer: () => latest.renderer,
              onAnimationHistory: (count) => reportStatus(
                count >= 240 ? "Scene ready; captured 24 seconds of transform animation" :
                  `Scene found; capturing animation ${Math.round(count / 10)} / 24 seconds`
              ),
              onError: (error) => {
                console.error("Three.js to Blender capture failed", error);
                void reportStatus("Scene capture failed", String(error));
              },
            });
          }
        });
      }
    }
    return originalRender.call(this, scene, camera, ...args);
  };
  Object.defineProperty(prototype, "render", {
    configurable: true,
    writable: true,
    value: wrappedRender,
  });
  Object.defineProperty(prototype, captureMarker, { value: true });
  window.addEventListener("beforeunload", () => sync?.(), { once: true });
  void reportStatus("Next/React Three.js capture adapter loaded; waiting for a render");
} else if (!prototype) {
  void reportStatus("Capture adapter loaded, but this app has no WebGLRenderer import", "Three.js module not found");
}
'''


def _install_react_runtime_capture(project_dir, receiver_port, token, framework):
    """Install a temporary renderer hook for Next.js or Create React App."""
    global _PREVIEW_GENERATED_FILES
    _remove_stale_react_capture(project_dir)
    source_root = os.path.join(project_dir, "src")
    if framework == "next":
        integration_root = project_dir
        if not (os.path.isdir(os.path.join(project_dir, "app")) or os.path.isdir(os.path.join(project_dir, "pages"))):
            if os.path.isdir(os.path.join(source_root, "app")) or os.path.isdir(os.path.join(source_root, "pages")):
                integration_root = source_root
        entry_candidates = [
            os.path.join(integration_root, f"instrumentation-client.{extension}")
            for extension in ("js", "ts")
        ]
    else:
        integration_root = source_root
        entry_candidates = [
            os.path.join(source_root, f"index.{extension}")
            for extension in ("tsx", "jsx", "ts", "js")
        ]
    if not os.path.isdir(integration_root):
        raise RuntimeError(f"Could not find the {framework} client source directory")

    helper_name = f"three_export_helper_blender_{receiver_port}_{token[:8]}.js"
    helper_path = os.path.join(integration_root, helper_name)
    helper_source = os.path.join(os.path.dirname(__file__), "three_export_helper.js")
    with open(helper_source, "r", encoding="utf-8") as helper_file:
        helper_contents = helper_file.read()
    _write_preview_file(helper_path, helper_contents)

    capture_name = f".threejs_blender_capture_{receiver_port}_{token[:8]}.js"
    capture_path = os.path.join(integration_root, capture_name)
    helper_import = "./" + helper_name
    capture_source = (
        _REACT_CAPTURE_SOURCE
        .replace("__HELPER_IMPORT__", json.dumps(helper_import))
        .replace("__RECEIVER_PORT__", str(int(receiver_port)))
        .replace("__TOKEN__", json.dumps(token))
    )
    _write_preview_file(capture_path, capture_source)

    if framework == "next":
        existing_entry = next((path for path in entry_candidates if os.path.isfile(path)), None)
        if existing_entry:
            relative_import = "./" + os.path.basename(capture_path)
            _patch_preview_import(existing_entry, f'import "{relative_import}";')
        else:
            entry_path = entry_candidates[0]
            relative_import = "./" + os.path.basename(capture_path)
            _write_preview_file(entry_path, f'import "{relative_import}";\n')
    else:
        entry_path = next((path for path in entry_candidates if os.path.isfile(path)), None)
        if entry_path is None:
            raise RuntimeError("Create React App capture needs src/index.js, src/index.jsx, src/index.ts, or src/index.tsx")
        relative_import = "./" + os.path.basename(capture_path)
        _patch_preview_import(entry_path, f'import "{relative_import}";')


def _vite_dependency_install_command(project_dir):
    """Install declared project dependencies when Vite is not resolvable locally."""
    current = os.path.abspath(project_dir)
    while True:
        if os.path.isdir(os.path.join(current, "node_modules", "vite")):
            return ""
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    package_path = os.path.join(project_dir, "package.json")
    with open(package_path, "r", encoding="utf-8") as package_file:
        package = json.load(package_file)
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if "vite" not in dependencies:
        raise RuntimeError("Vite is not installed and is not declared in this project's package.json")
    if os.path.isfile(os.path.join(project_dir, "pnpm-lock.yaml")):
        return "pnpm install"
    if os.path.isfile(os.path.join(project_dir, "yarn.lock")):
        return "yarn install"
    if os.path.isfile(os.path.join(project_dir, "bun.lock")) or os.path.isfile(os.path.join(project_dir, "bun.lockb")):
        return "bun install"
    return "npm install"


def _available_preview_port(requested_port):
    for port in range(requested_port, min(requested_port + 21, 65536)):
        probes = []
        try:
            ipv4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probes.append(ipv4)
            ipv4.bind(("0.0.0.0", port))
            if socket.has_ipv6:
                ipv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                probes.append(ipv6)
                ipv6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                ipv6.bind(("::", port))
            return port
        except OSError:
            continue
        finally:
            for probe in probes:
                probe.close()
    raise RuntimeError(f"No free preview port found starting at {requested_port}")


def _is_standard_dev_command(command):
    normalized = " ".join(command.strip().lower().split())
    return bool(re.fullmatch(
        r"(?:npm\s+run\s+dev|pnpm\s+(?:run\s+)?dev|yarn\s+dev|bun\s+run\s+dev)(?:\s+--.*)?",
        normalized,
    ))


def _open_preview_when_ready():
    global _PREVIEW_OPEN_URL, _PREVIEW_INSTALLING
    process = _PREVIEW_PROCESS
    if not _PREVIEW_OPEN_URL or process is None or process.poll() is not None:
        _PREVIEW_OPEN_URL = ""
        _PREVIEW_INSTALLING = False
        return None
    parsed = urllib.parse.urlparse(_PREVIEW_OPEN_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.2):
            bpy.ops.wm.url_open(url=_PREVIEW_OPEN_URL)
            _PREVIEW_OPEN_URL = ""
            _PREVIEW_INSTALLING = False
            return None
    except OSError:
        return 0.5


def _start_project_preview(window_manager):
    global _PREVIEW_PROCESS, _PREVIEW_LOG, _PREVIEW_CAPTURE_MODE, _PREVIEW_INSTALLING, _PREVIEW_LOG_PATH
    if _PREVIEW_PROCESS is not None and _PREVIEW_PROCESS.poll() is None:
        raise RuntimeError("The project preview server is already running")
    if _PREVIEW_LOG is not None:
        _PREVIEW_LOG.close()
        _PREVIEW_LOG = None
    project_dir = os.path.abspath(bpy.path.abspath(window_manager.threejs_project_path))
    if not os.path.isdir(project_dir):
        raise RuntimeError("Choose an existing project folder")
    command_override = window_manager.threejs_preview_command.strip()
    next_info = _next_project_info(project_dir)
    package_path = os.path.join(project_dir, "package.json")
    try:
        with open(package_path, "r", encoding="utf-8") as package_file:
            project_package = json.load(package_file)
    except (OSError, ValueError):
        project_package = {}
    project_scripts = project_package.get("scripts", {})
    project_dependencies = {**project_package.get("dependencies", {}), **project_package.get("devDependencies", {})}
    cra_script = any("react-scripts start" in str(project_scripts.get(key, "")).lower() for key in ("dev", "start"))
    standard_command = not command_override or _is_standard_dev_command(command_override)
    use_vite_capture = (
        _is_vite_project(project_dir)
        and standard_command
    )
    use_next_capture = bool(next_info and next_info.get("supported") and standard_command)
    use_cra_capture = bool(cra_script and standard_command and os.path.isdir(os.path.join(project_dir, "src")))
    command, suggested_url = _project_preview_command(project_dir, window_manager.threejs_preview_command)
    if suggested_url and window_manager.threejs_preview_url in {
        "http://127.0.0.1:5173", window_manager.threejs_auto_preview_url
    }:
        window_manager.threejs_preview_url = suggested_url
        window_manager.threejs_auto_preview_url = suggested_url
    if use_vite_capture:
        try:
            preview_url = urllib.parse.urlparse(window_manager.threejs_preview_url)
            preview_port = preview_url.port or 5173
        except ValueError:
            preview_url = urllib.parse.urlparse("http://127.0.0.1:5173")
            preview_port = 5173
        available_port = _available_preview_port(preview_port)
        if available_port != preview_port:
            host = preview_url.hostname or "127.0.0.1"
            netloc = f"[{host}]:{available_port}" if ":" in host else f"{host}:{available_port}"
            window_manager.threejs_preview_url = urllib.parse.urlunparse(preview_url._replace(netloc=netloc))
            if window_manager.threejs_auto_preview_url == preview_url.geturl():
                window_manager.threejs_auto_preview_url = window_manager.threejs_preview_url
            preview_port = available_port
        command = _install_vite_runtime_capture(
            project_dir,
            window_manager.threejs_runtime_port,
            preview_port,
            window_manager.threejs_runtime_token,
        )
        install_command = _vite_dependency_install_command(project_dir)
        if install_command:
            command = f"{install_command} && {command}"
        _PREVIEW_INSTALLING = bool(install_command)
        _PREVIEW_CAPTURE_MODE = "vite"
    elif use_next_capture:
        preview_url = urllib.parse.urlparse(window_manager.threejs_preview_url)
        preview_port = preview_url.port or 3000
        available_port = _available_preview_port(preview_port)
        if available_port != preview_port:
            host = preview_url.hostname or "127.0.0.1"
            netloc = f"[{host}]:{available_port}" if ":" in host else f"{host}:{available_port}"
            window_manager.threejs_preview_url = urllib.parse.urlunparse(preview_url._replace(netloc=netloc))
            preview_port = available_port
        _install_react_runtime_capture(
            project_dir, window_manager.threejs_runtime_port,
            window_manager.threejs_runtime_token, "next",
        )
        command = f"{command} --port {preview_port}"
        _PREVIEW_CAPTURE_MODE = "next"
    elif use_cra_capture:
        preview_url = urllib.parse.urlparse(window_manager.threejs_preview_url)
        preview_port = preview_url.port or 3000
        available_port = _available_preview_port(preview_port)
        if available_port != preview_port:
            host = preview_url.hostname or "127.0.0.1"
            netloc = f"[{host}]:{available_port}" if ":" in host else f"{host}:{available_port}"
            window_manager.threejs_preview_url = urllib.parse.urlunparse(preview_url._replace(netloc=netloc))
            preview_port = available_port
        _install_react_runtime_capture(
            project_dir, window_manager.threejs_runtime_port,
            window_manager.threejs_runtime_token, "cra",
        )
        command = re.sub(r"\s+--\s+--host(?:name)?\s+127\.0\.0\.1$", "", command)
        if os.name == "nt":
            command = f"set HOST=127.0.0.1&& set PORT={preview_port}&& {command}"
        else:
            command = f"HOST=127.0.0.1 PORT={preview_port} {command}"
        _PREVIEW_CAPTURE_MODE = "react"
    else:
        _PREVIEW_CAPTURE_MODE = "manual"
    log_path = os.path.join(tempfile.gettempdir(), "threejs-blender-preview.log")
    _PREVIEW_LOG_PATH = log_path
    _PREVIEW_LOG = open(log_path, "a", encoding="utf-8")
    options = {
        "cwd": project_dir,
        "shell": True,
        "stdout": _PREVIEW_LOG,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        _PREVIEW_PROCESS = subprocess.Popen(command, **options)
    except Exception:
        _PREVIEW_LOG.close()
        _PREVIEW_LOG = None
        raise
    return command, log_path


def _preview_exit_error():
    if _PREVIEW_PROCESS is None or _PREVIEW_PROCESS.poll() in (None, 0) or not _PREVIEW_LOG_PATH:
        return ""
    try:
        with open(_PREVIEW_LOG_PATH, "r", encoding="utf-8", errors="replace") as log_file:
            lines = log_file.readlines()[-60:]
    except OSError:
        return f"Preview process exited with code {_PREVIEW_PROCESS.returncode}"
    for line in reversed(lines):
        line = line.strip()
        if any(marker in line for marker in ("SyntaxError:", "Error:", "ERR_", "failed", "Failed")):
            return line[:140]
    return f"Preview process exited with code {_PREVIEW_PROCESS.returncode}"


def _stop_project_preview():
    global _PREVIEW_PROCESS, _PREVIEW_LOG, _PREVIEW_GENERATED_FILES, _PREVIEW_MODIFIED_FILES, _PREVIEW_CAPTURE_MODE, _PREVIEW_OPEN_URL, _PREVIEW_INSTALLING
    _PREVIEW_OPEN_URL = ""
    _PREVIEW_INSTALLING = False
    if bpy.app.timers.is_registered(_open_preview_when_ready):
        bpy.app.timers.unregister(_open_preview_when_ready)
    process = _PREVIEW_PROCESS
    _PREVIEW_PROCESS = None
    if process is not None and process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    if _PREVIEW_LOG is not None:
        _PREVIEW_LOG.close()
        _PREVIEW_LOG = None
    for source_path, original_contents, installed_contents in _PREVIEW_MODIFIED_FILES:
        try:
            with open(source_path, "rb") as source_file:
                current_contents = source_file.read()
            capture_import = re.compile(
                rb"(?m)^\s*import\s+['\"]\./?\.threejs_blender_capture_[A-Za-z0-9_-]+\.js['\"]\s*;?\s*(?:\r?\n|$)"
            )
            cleaned_contents = capture_import.sub(b"", current_contents)
            if cleaned_contents != current_contents:
                with open(source_path, "wb") as source_file:
                    source_file.write(cleaned_contents)
        except OSError:
            pass
    _PREVIEW_MODIFIED_FILES = []
    for generated_path, expected_contents in _PREVIEW_GENERATED_FILES:
        try:
            with open(generated_path, "rb") as generated_file:
                if generated_file.read() == expected_contents:
                    os.remove(generated_path)
        except OSError:
            pass
    _PREVIEW_GENERATED_FILES = []
    _PREVIEW_CAPTURE_MODE = ""


class IMPORT_SCENE_OT_threejs_json(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.threejs_json"
    bl_label = "Import Three.js Scene"
    bl_description = "Import Three.js JSON or glTF scene data"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(
        default="*.json;*.three.json;*.gltf;*.glb",
        options={"HIDDEN"},
    )

    convert_axes: BoolProperty(
        name="Three.js Y-Up → Blender Z-Up",
        description="Convert Three.js default Y-up coordinates into Blender Z-up coordinates",
        default=True,
    )

    global_scale: FloatProperty(
        name="Scale",
        description="Global import scale",
        default=1.0,
        min=0.000001,
        soft_max=1000.0,
    )

    import_materials: BoolProperty(
        name="Materials & PBR Maps",
        description="Import Three.js material values and locally available PBR texture maps",
        default=True,
    )

    keep_user_data: BoolProperty(
        name="Keep userData",
        description="Store Three.js userData as JSON in Blender custom properties",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "convert_axes")
        layout.prop(self, "global_scale")
        layout.prop(self, "import_materials")
        layout.prop(self, "keep_user_data")

    def execute(self, context):
        try:
            extension = Path(self.filepath).suffix.lower()
            if extension in {".gltf", ".glb"}:
                if not hasattr(bpy.ops.import_scene, "gltf"):
                    raise RuntimeError("Blender's glTF importer is unavailable. Enable the glTF 2.0 importer add-on.")
                before = set(bpy.data.objects)
                result = bpy.ops.import_scene.gltf(filepath=self.filepath)
                if "CANCELLED" in result:
                    return {"CANCELLED"}
                imported = set(bpy.data.objects) - before
                bpy.ops.object.select_all(action="DESELECT")
                root = None
                for obj in imported:
                    if obj.parent not in imported:
                        obj.scale = tuple(value * self.global_scale for value in obj.scale)
                        if root is None:
                            root = obj
                    obj.select_set(True)
                if root is not None:
                    context.view_layer.objects.active = root
                self.report({"INFO"}, f"Imported glTF scene: {os.path.basename(self.filepath)}")
                return {"FINISHED"}

            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            importer = ThreeJSONImportContext(
                context=context,
                json_data=data,
                filepath=self.filepath,
                convert_axes=self.convert_axes,
                global_scale=self.global_scale,
                import_materials=self.import_materials,
                keep_user_data=self.keep_user_data,
            )
            root = importer.run()

            bpy.ops.object.select_all(action="DESELECT")
            if root is not None:
                root.select_set(True)
                context.view_layer.objects.active = root

            self.report({"INFO"}, f"Imported Three.js JSON: {os.path.basename(self.filepath)}")
            return {"FINISHED"}

        except Exception as exc:
            self.report({"ERROR"}, f"Three.js import failed: {exc}")
            print("[Three.js JSON Importer]", repr(exc))
            return {"CANCELLED"}


class THREEJS_FH_json(bpy.types.FileHandler):
    bl_idname = "THREEJS_FH_json"
    bl_label = "Three.js JSON"
    bl_import_operator = "import_scene.threejs_json"
    bl_file_extensions = ".json;.three.json;.gltf;.glb"

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"


class THREEJS_OT_start_runtime_receiver(bpy.types.Operator):
    bl_idname = "threejs.start_runtime_receiver"
    bl_label = "Start Runtime Receiver"
    bl_description = "Listen for Three.js scenes sent from a running browser app"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            _validate_runtime_port(context.window_manager)
            token = _start_runtime_server(context.window_manager.threejs_runtime_port)
            context.window_manager.threejs_runtime_token = token
            self.report({"INFO"}, f"Three.js receiver listening on 127.0.0.1:{context.window_manager.threejs_runtime_port}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start Three.js receiver: {exc}")
            return {"CANCELLED"}


class THREEJS_OT_stop_runtime_receiver(bpy.types.Operator):
    bl_idname = "threejs.stop_runtime_receiver"
    bl_label = "Stop Runtime Receiver"
    bl_description = "Stop listening for browser scene exports"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _stop_runtime_server()
        context.window_manager.threejs_runtime_token = ""
        self.report({"INFO"}, "Three.js runtime receiver stopped")
        return {"FINISHED"}


class THREEJS_OT_toggle_runtime_receiver(bpy.types.Operator):
    bl_idname = "threejs.toggle_runtime_receiver"
    bl_label = "Three.js Live Preview"
    bl_description = "Start or stop the project preview and Three.js live receiver"

    def execute(self, context):
        if _RUNTIME_SERVER is None:
            if context.window_manager.threejs_project_path:
                return bpy.ops.threejs.start_project_preview()
            return bpy.ops.threejs.start_runtime_receiver()
        if _PREVIEW_PROCESS is not None and _PREVIEW_PROCESS.poll() is None:
            return bpy.ops.threejs.stop_project_preview()
        return bpy.ops.threejs.stop_runtime_receiver()


class THREEJS_OT_copy_runtime_token(bpy.types.Operator):
    bl_idname = "threejs.copy_runtime_token"
    bl_label = "Copy Token"
    bl_description = "Copy the runtime receiver token to the clipboard"

    def execute(self, context):
        context.window_manager.clipboard = context.window_manager.threejs_runtime_token
        self.report({"INFO"}, "Three.js receiver token copied")
        return {"FINISHED"}


class THREEJS_OT_start_project_preview(bpy.types.Operator):
    bl_idname = "threejs.start_project_preview"
    bl_label = "Start Project Preview"
    bl_description = "Start the selected web project and the Blender runtime receiver"

    def execute(self, context):
        global _PREVIEW_OPEN_URL
        window_manager = context.window_manager
        started_receiver = False
        try:
            _validate_runtime_port(window_manager)
            if _RUNTIME_SERVER is None:
                window_manager.threejs_runtime_token = _start_runtime_server(window_manager.threejs_runtime_port)
                started_receiver = True
            command, log_path = _start_project_preview(window_manager)
            _PREVIEW_OPEN_URL = window_manager.threejs_preview_url
            if not bpy.app.timers.is_registered(_open_preview_when_ready):
                bpy.app.timers.register(_open_preview_when_ready, first_interval=0.5)
            self.report({"INFO"}, f"Preview started: {command}. Log: {log_path}")
            return {"FINISHED"}
        except Exception as exc:
            _stop_project_preview()
            if started_receiver:
                _stop_runtime_server()
                window_manager.threejs_runtime_token = ""
            self.report({"ERROR"}, f"Could not start project preview: {exc}")
            return {"CANCELLED"}


class THREEJS_OT_stop_project_preview(bpy.types.Operator):
    bl_idname = "threejs.stop_project_preview"
    bl_label = "Stop Project Preview"
    bl_description = "Stop the project server and Blender runtime receiver"

    def execute(self, context):
        _stop_project_preview()
        _stop_runtime_server()
        context.window_manager.threejs_runtime_token = ""
        self.report({"INFO"}, "Project preview and runtime receiver stopped")
        return {"FINISHED"}


class THREEJS_OT_import_runtime_scene(bpy.types.Operator):
    bl_idname = "threejs.import_runtime_scene"
    bl_label = "Import Latest Scene"
    bl_description = "Import the latest scene captured from the running Three.js project"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, _context):
        return not _RUNTIME_QUEUE.empty()

    def execute(self, _context):
        try:
            scene_name = _import_queued_runtime_scene()
            self.report({"INFO"}, f"Imported Three.js scene: {scene_name}")
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Could not import captured scene: {exc}")
            return {"CANCELLED"}


class THREEJS_PT_runtime_receiver(bpy.types.Panel):
    bl_label = "Three.js Runtime"
    bl_idname = "THREEJS_PT_runtime_receiver"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Three.js"

    def draw(self, context):
        layout = self.layout
        window_manager = context.window_manager
        layout.prop(window_manager, "threejs_project_path")
        layout.prop(window_manager, "threejs_preview_command")
        layout.prop(window_manager, "threejs_preview_url")
        layout.label(text="Preview URL is the web app (for example, Vite on port 5173)")
        layout.label(text="Automatic runtime capture for Vite and supported React dev projects")
        if _PREVIEW_PROCESS is not None and _PREVIEW_PROCESS.poll() is None:
            if _PREVIEW_INSTALLING:
                layout.label(text="Installing dependencies, then starting preview", icon="TIME")
            else:
                layout.label(text="Project server running", icon="PLAY")
            layout.operator(THREEJS_OT_stop_project_preview.bl_idname, icon="PAUSE")
        else:
            preview_error = _preview_exit_error()
            if preview_error:
                error_box = layout.box()
                error_box.alert = True
                error_box.label(text="Preview server stopped", icon="ERROR")
                error_box.label(text=preview_error)
            layout.operator(THREEJS_OT_start_project_preview.bl_idname, icon="URL")
        layout.separator()
        layout.label(text="Runtime receiver", icon="NETWORK_DRIVE")
        if _RUNTIME_SERVER is None:
            layout.prop(window_manager, "threejs_runtime_port", text="Receiver Port")
            layout.label(text="Use a separate port, usually 8765")
            layout.operator(THREEJS_OT_start_runtime_receiver.bl_idname, icon="PLAY")
        else:
            layout.label(text=f"Listening on 127.0.0.1:{_RUNTIME_PORT}", icon="NETWORK_DRIVE")
            if _RUNTIME_QUEUE.empty():
                if _RUNTIME_LAST_ERROR:
                    error_box = layout.box()
                    error_box.alert = True
                    error_box.label(text="Runtime capture failed", icon="ERROR")
                    error_box.label(text=_RUNTIME_LAST_ERROR[:100])
                elif _RUNTIME_LAST_STATUS:
                    layout.label(text=_RUNTIME_LAST_STATUS[:70], icon="INFO")
                else:
                    layout.label(text="Waiting for the project to render a scene")
            else:
                layout.label(text="Latest scene captured and ready to import", icon="CHECKMARK")
            if _RUNTIME_LAST_IMPORT_REPORT:
                layout.label(text=_RUNTIME_LAST_IMPORT_REPORT[:110], icon="ANIM_DATA")
            row = layout.row(align=True)
            row.enabled = False
            row.prop(window_manager, "threejs_runtime_token", text="Auth token")
            if _PREVIEW_CAPTURE_MODE in {"vite", "next", "react"}:
                bridge_name = {"vite": "Vite", "next": "Next.js", "react": "React"}[_PREVIEW_CAPTURE_MODE]
                layout.label(text=f"{bridge_name} bridge authorizes automatically")
            else:
                layout.label(text="Required for manual scene senders")
                layout.operator(THREEJS_OT_copy_runtime_token.bl_idname, text="Copy Token", icon="COPYDOWN")
            import_row = layout.row()
            import_row.enabled = not _RUNTIME_QUEUE.empty()
            import_row.operator(THREEJS_OT_import_runtime_scene.bl_idname, text="Import Latest Scene", icon="IMPORT")
            layout.operator(THREEJS_OT_stop_runtime_receiver.bl_idname, icon="PAUSE")


def draw_threejs_header_badge(self, context):
    if context.area is None or context.area.type != "VIEW_3D":
        return
    running = _RUNTIME_SERVER is not None
    self.layout.operator(
        THREEJS_OT_toggle_runtime_receiver.bl_idname,
        text="Three.js LIVE" if running else "Three.js",
        icon="REC" if running else "PLUGIN",
        depress=running,
    )


def menu_func_import(self, context):
    self.layout.operator(
        IMPORT_SCENE_OT_threejs_json.bl_idname,
        text="Three.js Scene (.json, .gltf, .glb)",
    )


classes = (
    IMPORT_SCENE_OT_threejs_json,
    THREEJS_FH_json,
    THREEJS_OT_start_runtime_receiver,
    THREEJS_OT_stop_runtime_receiver,
    THREEJS_OT_toggle_runtime_receiver,
    THREEJS_OT_copy_runtime_token,
    THREEJS_OT_start_project_preview,
    THREEJS_OT_stop_project_preview,
    THREEJS_OT_import_runtime_scene,
    THREEJS_PT_runtime_receiver,
)


def register():
    bpy.types.WindowManager.threejs_project_path = StringProperty(
        name="Project Folder", description="Folder containing the Three.js app", subtype="DIR_PATH", default=""
    )
    bpy.types.WindowManager.threejs_preview_command = StringProperty(
        name="Preview Command", description="Optional shell command; empty auto-detects npm or static HTML", default=""
    )
    bpy.types.WindowManager.threejs_preview_url = StringProperty(
        name="Preview URL", description="Web app URL opened in the browser; separate from the receiver port", default="http://127.0.0.1:5173"
    )
    bpy.types.WindowManager.threejs_auto_preview_url = StringProperty(
        name="Auto Preview URL", default="http://127.0.0.1:5173", options={"HIDDEN"}
    )
    bpy.types.WindowManager.threejs_runtime_port = IntProperty(
        name="Receiver Port", description="Local scene receiver port; use a port separate from the web preview, usually 8765", default=8765, min=1024, max=65535
    )
    bpy.types.WindowManager.threejs_runtime_token = StringProperty(
        name="Bearer token", description="Copy this token to your Three.js sender", default=""
    )
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_HT_header.append(draw_threejs_header_badge)


def unregister():
    _stop_project_preview()
    _stop_runtime_server()
    bpy.types.VIEW3D_HT_header.remove(draw_threejs_header_badge)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.threejs_runtime_token
    del bpy.types.WindowManager.threejs_runtime_port
    del bpy.types.WindowManager.threejs_preview_url
    del bpy.types.WindowManager.threejs_auto_preview_url
    del bpy.types.WindowManager.threejs_preview_command
    del bpy.types.WindowManager.threejs_project_path
