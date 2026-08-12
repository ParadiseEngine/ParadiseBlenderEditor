"""Full scene export inside Blender, with contract assertions.

Builds a small scene covering every exported feature, exports it, and checks the resulting
document. The assertions are mostly about the **axis conversion surviving each path** -- entity
transforms, camera, lights, collider poses -- because that is where a Blender host differs from
the Godot one and where a regression would be silent.

Documents land in ``$TMPDIR/paradise_export_test`` so ``tools/run_tests.sh`` can then feed
them to the .NET conformance gate.

Run with::

    blender --background --factory-startup --python tests/integration/test_export_scene.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.export.scene import export_scene  # noqa: E402

DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_export_test")

failures: list[str] = []


def check(condition: bool, description: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {description}")
    else:
        print(f"FAIL {description}{(' — ' + detail) if detail else ''}")
        failures.append(description)


def approx(actual, expected, tolerance=1e-4) -> bool:
    if isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True)
        )
    return abs(actual - expected) <= tolerance


def build_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.paradise_project.data_dir = DATA_DIR
    scene.paradise_project.scene_name_override = "export_test"
    scene.paradise_project.export_on_save = False

    # Ground: a wide flat box with a collider, so it becomes walkable navmesh geometry.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.name = "Ground"
    ground.scale = (20.0, 20.0, 0.5)
    ground.paradise.is_entity = True

    material = bpy.data.materials.new("mat_ground")
    material.use_nodes = True
    bsdf = material.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.25, 0.5, 0.75, 1.0)
    bsdf.inputs["Metallic"].default_value = 0.25
    bsdf.inputs["Roughness"].default_value = 0.75
    ground.data.materials.append(material)

    bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
    collider = bpy.context.active_object
    collider.name = "GroundCollider"
    collider.paradise_collider.is_collider = True
    collider.paradise_collider.shape = "Box"
    collider.paradise_collider.layer = 1
    collider.parent = ground
    ground.paradise.physics_colliders.add().target = collider

    # A dynamic prop, placed off-axis so the conversion is observable.
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(1.0, 2.0, 3.0))
    ball = bpy.context.active_object
    ball.name = "Ball"
    ball.paradise.is_entity = True
    ball.paradise.is_dynamic_body = True
    ball.paradise.body_mass = 2.5

    bpy.ops.object.empty_add(type="SPHERE", location=(1.0, 2.0, 3.0))
    ball_collider = bpy.context.active_object
    ball_collider.name = "BallCollider"
    ball_collider.paradise_collider.is_collider = True
    ball_collider.paradise_collider.shape = "Sphere"
    ball_collider.parent = ball
    ball.paradise.physics_colliders.add().target = ball_collider

    # An agent, which must be excluded from the navmesh bake.
    bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=1.8, location=(-3.0, 0.0, 1.0))
    agent = bpy.context.active_object
    agent.name = "Hero"
    agent.paradise.is_entity = True
    agent.paradise.is_agent = True
    agent.paradise.move_speed = 3.0

    # Camera at the engine's documented validation pose.
    bpy.ops.object.camera_add(location=(0, -10, 1), rotation=(math.radians(90), 0, 0))
    scene.camera = bpy.context.active_object
    scene.camera.name = "MainCamera"

    bpy.ops.object.light_add(type="SUN", location=(0, 0, 10))
    sun = bpy.context.active_object
    sun.name = "Sun"
    sun.data.energy = 3.0

    bpy.ops.object.light_add(type="SPOT", location=(2, 0, 5))
    spot = bpy.context.active_object
    spot.name = "Spot"
    spot.data.energy = 200.0
    spot.data.spot_size = math.radians(60)

    world = bpy.data.worlds.new("World")
    scene.world = world
    scene.view_settings.view_transform = "AgX"


def main() -> int:
    paradise_blender.register()
    build_scene()

    output = export_scene(bpy.context.scene)
    if output is None or not os.path.exists(output):
        print("FAIL export produced no document")
        return 1

    with open(output, encoding="utf-8") as handle:
        document = json.load(handle)

    print()
    check(document["SchemaVersion"] == 2, "schema version is 2")

    entities = {e["Id"]: e for e in document["Entities"]}
    check(set(entities) == {"Ball", "Ground", "Hero"}, "all three entities exported",
          str(sorted(entities)))

    # -- axis conversion --------------------------------------------------------------
    ball = entities["Ball"]
    check(approx(ball["LocalPosition"], [1.0, 3.0, -2.0]),
          "Blender (1, 2, 3) converts to contract (1, 3, -2)", str(ball["LocalPosition"]))
    check(approx(ball["WorldMatrix"][12:15], [1.0, 3.0, -2.0]),
          "world matrix translation lands at flat indices 12/13/14", str(ball["WorldMatrix"][12:15]))

    ground = entities["Ground"]
    check(approx(ground["LocalScale"], [20.0, 0.5, 20.0]),
          "Blender scale (20, 20, 0.5) converts to contract (20, 0.5, 20)", str(ground["LocalScale"]))

    camera = document["Camera"]
    check(approx(camera["Position"], [0.0, 1.0, 10.0]),
          "camera at Blender (0, -10, 1) exports as contract (0, 1, 10)", str(camera["Position"]))

    # -- entity identity --------------------------------------------------------------
    guids = {e["EntityGuid"] for e in document["Entities"]}
    check(len(guids) == 3, "every entity has a distinct GUID")
    check(all(g != "00000000-0000-0000-0000-000000000000" for g in guids),
          "no entity exported the all-zero GUID")

    # -- components -------------------------------------------------------------------
    check(ball["Components"]["Rigidbody"]["BodyType"] == "Dynamic",
          "the dynamic prop exports BodyType Dynamic")
    check(approx(ball["Components"]["Rigidbody"]["Mass"], 2.5), "dynamic body mass carries through")
    check(ground["Components"]["Rigidbody"]["BodyType"] == "Static",
          "an ordinary collidable entity exports BodyType Static")
    check(approx(ground["Components"]["Rigidbody"]["Mass"], 0.0),
          "a static body exports zero mass")

    hero = entities["Hero"]
    check(hero["Components"]["Agent"] is not None, "the agent exports an Agent component")
    check(approx(hero["Components"]["Agent"]["MoveSpeed"], 3.0), "agent move speed carries through")
    check(hero["Components"]["Agent"]["IdleClip"] == "Idle",
          "an unset idle clip falls back to the shared default")

    shapes = ground["Components"]["Collider"]["Colliders"]
    check(len(shapes) == 1 and shapes[0]["ShapeType"] == "Box", "the box collider exported")
    check(shapes[0]["Layer"] == 1, "the collider's layer index carries through")
    check(all(s > 0 for s in shapes[0]["Size"]),
          "the collider derived a non-zero size from an empty's display bounds", str(shapes[0]["Size"]))

    sphere_shape = ball["Components"]["Collider"]["Colliders"][0]
    check(sphere_shape["ShapeType"] == "Sphere" and sphere_shape["Radius"] > 0,
          "the sphere collider exported with a radius")

    check(ground["Components"]["Renderable"]["Mesh"] is not None, "the ground references a mesh GLB")
    check(ground["Materials"] == ["materials/mat_ground.json"],
          "the material slot references its contract field", str(ground["Materials"]))

    # -- lighting and environment -----------------------------------------------------
    lights = {light["Id"]: light for light in document["Lighting"]["States"][0]["Lights"]}
    check(set(lights) == {"Spot", "Sun"}, "both lights exported", str(sorted(lights)))
    check(lights["Sun"]["Type"] == "Directional", "a sun lamp exports as Directional")
    check(approx(lights["Sun"]["Direction"], [0.0, -1.0, 0.0]),
          "an unrotated sun points down in contract axes", str(lights["Sun"]["Direction"]))
    check(approx(lights["Sun"]["Intensity"], 3.0), "sun energy passes through unscaled")
    check(lights["Spot"]["Type"] == "Spot", "a spot lamp exports as Spot")
    check(approx(lights["Spot"]["SpotAngle"], 60.0),
          "Blender's spot_size is already the full cone angle and is not doubled",
          str(lights["Spot"]["SpotAngle"]))
    check(approx(lights["Spot"]["Intensity"], 2.0),
          "200 W maps to intensity 2 via the documented calibration",
          str(lights["Spot"]["Intensity"]))

    environment = document["Lighting"]["States"][0]["Environment"]
    check(environment["TonemapMode"] == "Agx", "the AgX view transform maps to the Agx operator")

    # -- navmesh ----------------------------------------------------------------------
    # The walkable-geometry filter must exclude DYNAMIC bodies: the ball is collidable but
    # moves, and baking it would freeze a bump into the walkable surface at its spawn (the
    # Godot host bakes StaticColliders only — same semantics). Ball world position converted
    # to contract axes is (1, 3, -2); nothing in the collected geometry may be near it.
    from paradise_blender.export.navmesh import collect_walkable_geometry

    nav_vertices, nav_triangles = collect_walkable_geometry(bpy.context.scene)
    check(len(nav_triangles) > 0, "static collidable geometry reaches the navmesh bake")
    ball_near = any(
        abs(nav_vertices[i] - 1.0) < 0.6
        and abs(nav_vertices[i + 1] - 3.0) < 0.6
        and abs(nav_vertices[i + 2] - -2.0) < 0.6
        for i in range(0, len(nav_vertices), 3)
    )
    check(not ball_near, "the dynamic ball is excluded from the walkable geometry")

    if document["NavMeshFile"]:
        navmesh = os.path.join(DATA_DIR, "scenes", document["NavMeshFile"])
        check(os.path.exists(navmesh) and os.path.getsize(navmesh) > 0,
              "the navmesh binary was baked and is non-empty")
    else:
        print("     (navmesh skipped — the .NET bridge was unavailable)")

    # -- side artifacts ---------------------------------------------------------------
    check(os.path.exists(os.path.join(DATA_DIR, "ProjectSettings.json")),
          "ProjectSettings.json was written")
    check(os.path.exists(os.path.join(DATA_DIR, "materials", "mat_ground.json")),
          "the material document was written")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All checks passed. Documents in {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
