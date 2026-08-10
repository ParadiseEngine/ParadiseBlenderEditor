using System.Text.Json;

namespace ParadiseBlenderBridge;

/// <summary>
/// The .NET half of the Blender addon. Two verbs, both optional to ordinary use:
///
/// <code>
///   ParadiseBlenderBridge navmesh --input geom.json --output scene.navmesh.bin
///   ParadiseBlenderBridge contract-check scene.json [--verbose]
/// </code>
///
/// <b>navmesh</b> bakes walkable geometry with DotRecast and writes the runtime's MeshSet
/// binary. This lives here because DotRecast is C#-only; the Godot host gets its bake from
/// Godot's own NavigationServer3D, and Blender has no equivalent.
///
/// <b>contract-check</b> is the drift gate. The Blender addon writes the contract in pure
/// Python, so nothing structurally guarantees it stays in step with C# <c>Paradise.Export</c>
/// as the schema evolves. This reads a Python-produced document with the engine's own
/// <c>ExportJsonReader</c>, re-serializes it with <c>ExportJsonWriter</c>, and compares the
/// two semantically. Any key the reader ignored, any enum spelled wrong, any value that did
/// not survive the round trip shows up here.
/// </summary>
internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            return Usage();
        }

        try
        {
            return args[0] switch
            {
                "navmesh" => NavMeshCommand.Run(args[1..]),
                "contract-check" => ContractCheckCommand.Run(args[1..]),
                "--help" or "-h" or "help" => Usage(),
                _ => Fail($"Unknown verb '{args[0]}'."),
            };
        }
        catch (JsonException ex)
        {
            // A malformed document is by far the most likely failure and deserves a message
            // that points at the file rather than a stack trace.
            return Fail($"Malformed JSON: {ex.Message}");
        }
        catch (Exception ex)
        {
            return Fail(ex.Message);
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine(
            """
            ParadiseBlenderBridge — the .NET half of the Paradise Blender addon.

              navmesh --input <geometry.json> --output <scene.navmesh.bin>
                  Bake walkable geometry into the runtime's DotRecast MeshSet binary.
                  The input is written by the addon: { vertices: [x,y,z,...],
                  triangles: [i,j,k,...], settings: { cellSize, cellHeight, agentHeight,
                  agentRadius, agentMaxClimb } } in contract space (Y-up).

              contract-check <document.json> [--verbose]
                  Round-trip a Python-written contract document through the engine's own
                  reader and writer, and report any semantic difference. Exit code 1 means
                  the Python contract implementation has drifted.
            """);
        return 2;
    }

    internal static int Fail(string message)
    {
        Console.Error.WriteLine($"[ParadiseBlenderBridge] {message}");
        return 1;
    }

    internal static string? Option(string[] args, string name)
    {
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == name)
            {
                return args[i + 1];
            }
        }

        return null;
    }
}
