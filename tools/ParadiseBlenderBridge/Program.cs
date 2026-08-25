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
/// <b>engine-schema</b> prints <c>Paradise.Export.AuthoringSchema.Json</c> — the engine's own
/// authored-component schema. The addon no longer vendors a copy of it: a game's launcher merges
/// every assembly it references into the document it dumps, so the engine's components reach the
/// addon inside the GAME's schema, described by the engine that game actually builds against.
/// This verb is now a diagnostic — what does the engine publish, before any game merges it — for
/// when a component is missing from a panel and you need to know which half is at fault.
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
                "engine-schema" => PrintEngineSchema(),
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

              engine-schema
                  Print the engine's authored-component schema (the generated
                  Paradise.Export.AuthoringSchema.Json constant). A diagnostic: the addon reads
                  the GAME's dumped schema, which already carries these components because the
                  launcher that dumps it scans its references.
            """);
        return 2;
    }

    private static int PrintEngineSchema()
    {
        Console.WriteLine(Paradise.Export.AuthoringSchema.Json);
        return 0;
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
