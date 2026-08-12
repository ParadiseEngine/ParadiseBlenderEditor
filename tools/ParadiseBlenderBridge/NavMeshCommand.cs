using System.Numerics;
using System.Text.Json;
using System.Text.Json.Serialization;
using DotRecast.Core.Numerics;
using DotRecast.Recast;
using DotRecast.Recast.Geom;
using Paradise.Export.NavMesh;

namespace ParadiseBlenderBridge;

/// <summary>
/// Bakes walkable geometry into the runtime's navmesh binary.
///
/// The Godot host gets its triangulation from Godot's <c>NavigationServer3D</c> and only uses
/// <see cref="NavMeshBinaryWriter"/> to serialize it. Blender has no navmesh baker, so this
/// runs Recast directly — the same library Godot wraps — and then hands the resulting polygon
/// mesh to the same writer. Both hosts therefore produce the same binary format from the same
/// bake parameters.
/// </summary>
internal static class NavMeshCommand
{
    /// <summary>Recast's conventional "walkable ground" area id (0 is RC_NULL_AREA).</summary>
    private const int WalkableAreaId = 63;

    public static int Run(string[] args)
    {
        string? inputPath = Program.Option(args, "--input");
        string? outputPath = Program.Option(args, "--output");

        if (inputPath is null || outputPath is null)
        {
            return Program.Fail("navmesh requires --input <geometry.json> and --output <file.bin>.");
        }

        if (!File.Exists(inputPath))
        {
            return Program.Fail($"Input geometry '{inputPath}' does not exist.");
        }

        GeometryInput? input = JsonSerializer.Deserialize(
            File.ReadAllText(inputPath), GeometryJsonContext.Default.GeometryInput);

        if (input is null || input.Vertices.Length == 0 || input.Triangles.Length == 0)
        {
            return Program.Fail("Input geometry is empty; nothing to bake.");
        }

        if (input.Vertices.Length % 3 != 0 || input.Triangles.Length % 3 != 0)
        {
            return Program.Fail(
                "Input geometry is malformed: vertices and triangles must both be flat arrays " +
                "with a length divisible by 3.");
        }

        BakeSettings settings = input.Settings ?? new BakeSettings();

        RcPolyMesh? polyMesh = Bake(input, settings);
        if (polyMesh is null || polyMesh.npolys == 0)
        {
            // Not an error: geometry can be entirely non-walkable (all vertical, or smaller
            // than the agent). The addon leaves NavMeshFile null and the scene still loads.
            Console.Error.WriteLine(
                "[ParadiseBlenderBridge] Recast produced no walkable polygons. Check that the " +
                "geometry has surfaces flatter than the max slope and wider than the agent radius.");
            return 1;
        }

        (List<Vector3> vertices, List<int> triangles) = Triangulate(polyMesh);

        long bytes = NavMeshBinaryWriter.Write(
            outputPath, vertices, triangles,
            message => Console.Error.WriteLine($"[ParadiseBlenderBridge] {message}"));

        Console.WriteLine(
            $"Baked {polyMesh.npolys} polygons -> {triangles.Count / 3} triangles, {bytes} bytes.");
        return 0;
    }

    private static RcPolyMesh? Bake(GeometryInput input, BakeSettings settings)
    {
        float[] vertices = input.Vertices;
        int[] triangles = input.Triangles;

        var geometry = new RcSampleInputGeomProvider(vertices, triangles);

        var config = new RcConfig(
            partitionType: RcPartition.WATERSHED,
            cellSize: settings.CellSize,
            cellHeight: settings.CellHeight,
            agentMaxSlope: settings.AgentMaxSlope,
            agentHeight: settings.AgentHeight,
            agentRadius: settings.AgentRadius,
            agentMaxClimb: settings.AgentMaxClimb,
            regionMinSize: 8,
            regionMergeSize: 20,
            edgeMaxLen: 12f,
            edgeMaxError: 1.3f,
            // The contract's navmesh is a TRIANGLE mesh: NavMeshBinaryWriter's writer emits
            // 3 verts per poly, so asking Recast for larger polygons would only force us to
            // fan-triangulate more aggressively below.
            vertsPerPoly: 3,
            detailSampleDist: 6f,
            detailSampleMaxError: 1f,
            filterLowHangingObstacles: true,
            filterLedgeSpans: true,
            filterWalkableLowHeightSpans: true,
            // Mark every walkable span with the default "ground" area. The contract has no
            // per-area cost model, so a single area is all the runtime can act on.
            walkableAreaMod: new RcAreaModification(WalkableAreaId),
            buildMeshDetail: false);

        // Recast voxelizes into a heightfield sized by these bounds, and it needs vertical room
        // above the walkable surface: `filterWalkableLowHeightSpans` rejects any span without
        // agentHeight of clearance. Perfectly flat ground -- the most common case by far -- has
        // zero Y extent, which yields a zero-height heightfield and silently bakes nothing. So
        // the bounds are expanded to guarantee headroom plus a small floor margin.
        RcVec3f boundsMin = geometry.GetMeshBoundsMin();
        RcVec3f boundsMax = geometry.GetMeshBoundsMax();
        float headroom = settings.AgentHeight + settings.AgentMaxClimb;
        boundsMin = new RcVec3f(boundsMin.X, boundsMin.Y - settings.AgentMaxClimb, boundsMin.Z);
        boundsMax = new RcVec3f(boundsMax.X, boundsMax.Y + headroom, boundsMax.Z);

        var builderConfig = new RcBuilderConfig(config, boundsMin, boundsMax);

        RcBuilderResult result = new RcBuilder().Build(geometry, builderConfig, keepInterResults: false);
        return result.Mesh;
    }

    /// <summary>
    /// Fan-triangulate the polygon mesh into the vertex/index form
    /// <see cref="NavMeshBinaryWriter"/> consumes, dequantizing Recast's integer vertices.
    /// </summary>
    private static (List<Vector3>, List<int>) Triangulate(RcPolyMesh mesh)
    {
        var vertices = new List<Vector3>(mesh.nverts);
        var triangles = new List<int>(mesh.npolys * 3);

        RcVec3f origin = mesh.bmin;
        for (int i = 0; i < mesh.nverts; i++)
        {
            // Recast stores vertices as cell indices relative to the mesh bounds.
            vertices.Add(new Vector3(
                origin.X + mesh.verts[i * 3 + 0] * mesh.cs,
                origin.Y + mesh.verts[i * 3 + 1] * mesh.ch,
                origin.Z + mesh.verts[i * 3 + 2] * mesh.cs));
        }

        int stride = mesh.nvp * 2;
        for (int poly = 0; poly < mesh.npolys; poly++)
        {
            int baseIndex = poly * stride;

            // A polygon's vertex list is padded to nvp with RC_MESH_NULL_IDX.
            var corners = new List<int>(mesh.nvp);
            for (int i = 0; i < mesh.nvp; i++)
            {
                int index = mesh.polys[baseIndex + i];
                if (index == RcRecast.RC_MESH_NULL_IDX)
                {
                    break;
                }

                corners.Add(index);
            }

            // Emit the fan in Recast's own vertex order. Recast and Detour are one pipeline:
            // RcPolyMesh polygons are already wound the way DtNavMesh requires (CCW viewed
            // from +Y), so the order passes through VERBATIM. The Godot host reverses ITS fan
            // because Godot's NavigationServer triangulation is wound the other way — copying
            // that reversal here flipped these polys to clockwise, and the symptom is subtle:
            // the mesh loads and FindNearestPoly works, but FindStraightPath's funnel gets its
            // portal left/right swapped and returns zig-zag corridors, while Raycast reports
            // an immediate hit (t=0) on open ground.
            for (int i = 2; i < corners.Count; i++)
            {
                triangles.Add(corners[0]);
                triangles.Add(corners[i - 1]);
                triangles.Add(corners[i]);
            }
        }

        return (vertices, triangles);
    }

    internal sealed class GeometryInput
    {
        [JsonPropertyName("vertices")]
        public float[] Vertices { get; set; } = [];

        [JsonPropertyName("triangles")]
        public int[] Triangles { get; set; } = [];

        [JsonPropertyName("settings")]
        public BakeSettings? Settings { get; set; }
    }

    /// <summary>
    /// Bake parameters. Defaults mirror the Godot host's <c>NavMeshBake.cs</c> exactly, so the
    /// same scene bakes to the same navmesh from either authoring tool. The agent radius in
    /// particular is not a placeholder: at radius 0 the walkable area is not eroded, planned
    /// paths run flush against obstacle faces, and agent capsules grind along walls.
    /// </summary>
    internal sealed class BakeSettings
    {
        [JsonPropertyName("cellSize")]
        public float CellSize { get; set; } = 0.1f;

        [JsonPropertyName("cellHeight")]
        public float CellHeight { get; set; } = 0.1f;

        [JsonPropertyName("agentHeight")]
        public float AgentHeight { get; set; } = 1.8f;

        [JsonPropertyName("agentRadius")]
        public float AgentRadius { get; set; } = 0.4f;

        [JsonPropertyName("agentMaxClimb")]
        public float AgentMaxClimb { get; set; } = 0.3f;

        [JsonPropertyName("agentMaxSlope")]
        public float AgentMaxSlope { get; set; } = 45f;
    }
}

[JsonSerializable(typeof(NavMeshCommand.GeometryInput))]
[JsonSerializable(typeof(NavMeshCommand.BakeSettings))]
internal partial class GeometryJsonContext : JsonSerializerContext
{
}
