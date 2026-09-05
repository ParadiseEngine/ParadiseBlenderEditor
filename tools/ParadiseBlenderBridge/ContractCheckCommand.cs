using System.Text.Json;
using System.Text.Json.Nodes;
using Paradise.Export.Data;
using Paradise.Export.Serialization;

namespace ParadiseBlenderBridge;

/// <summary>
/// The conformance gate on the Blender addon's pure-Python contract implementation.
///
/// Reads a Python-written document with the engine's own <see cref="ExportJsonReader"/>,
/// re-serializes it with <see cref="ExportJsonWriter"/>, and compares the two documents
/// semantically. Anything the Python side got wrong shows up as a difference: a misspelled
/// key (the reader silently ignores it, so it vanishes on the way out), an enum written as a
/// number, a missing field, a wrong nesting level, a value that does not survive the round
/// trip.
///
/// The comparison is <b>value-based, not byte-based</b>, matching the engine's own stated
/// contract (see its CONVENTIONS.md): numbers compare as float32, so <c>5</c> and <c>5.0</c>
/// are the same value and only a real difference fails. Textual differences are reported
/// separately as informational, because matching System.Text.Json's spelling exactly is a
/// nice-to-have for hand-diffing, not a correctness requirement.
/// </summary>
internal static class ContractCheckCommand
{
    public static int Run(string[] args)
    {
        string? path = args.FirstOrDefault(a => !a.StartsWith("--", StringComparison.Ordinal));
        bool verbose = args.Contains("--verbose");

        if (path is null)
        {
            return Program.Fail("contract-check requires a document path.");
        }

        if (!File.Exists(path))
        {
            return Program.Fail($"'{path}' does not exist.");
        }

        string original = File.ReadAllText(path);
        string kind = DetectKind(path, original);

        string roundTripped;
        try
        {
            roundTripped = kind switch
            {
                "level" => ExportJsonWriter.SerializeToString(ExportJsonReader.ReadPrefab(original)),
                "material" => ExportJsonWriter.SerializeToString(ExportJsonReader.ReadMaterial(original)),
                "settings" => ExportJsonWriter.SerializeToString(
                    ExportJsonReader.ReadProjectSettings(original)),
                _ => throw new InvalidOperationException(
                    $"Cannot tell what kind of contract document '{path}' is. Expected a level " +
                    "(scenes/*.json), a material (materials/*.json), or ProjectSettings.json."),
            };
        }
        catch (JsonException ex)
        {
            return Program.Fail(
                $"The engine's reader rejected '{path}': {ex.Message}\n" +
                "This means the Python contract writer produced something the engine cannot parse.");
        }

        JsonNode? before = JsonNode.Parse(original);
        JsonNode? after = JsonNode.Parse(roundTripped);

        var differences = new List<string>();
        Compare(before, after, "$", differences);

        if (differences.Count == 0)
        {
            Console.WriteLine($"OK — '{Path.GetFileName(path)}' ({kind}) round-trips exactly.");
            if (verbose && !original.TrimEnd().Equals(roundTripped.TrimEnd(), StringComparison.Ordinal))
            {
                Console.WriteLine(
                    "Note: the documents are semantically identical but differ textually " +
                    "(number formatting or key order). That is allowed — the contract is " +
                    "value-based.");
            }

            return 0;
        }

        Console.Error.WriteLine(
            $"DRIFT — '{Path.GetFileName(path)}' does not survive a round trip through " +
            $"Paradise.Export ({differences.Count} difference(s)):");
        foreach (string difference in differences.Take(verbose ? int.MaxValue : 40))
        {
            Console.Error.WriteLine($"  {difference}");
        }

        if (!verbose && differences.Count > 40)
        {
            Console.Error.WriteLine($"  … and {differences.Count - 40} more (pass --verbose).");
        }

        return 1;
    }

    private static string DetectKind(string path, string json)
    {
        string name = Path.GetFileName(path);
        if (name.Equals("ProjectSettings.json", StringComparison.OrdinalIgnoreCase))
        {
            return "settings";
        }

        // Fall back to shape rather than to the directory name, so the check works on a
        // document handed over from anywhere (a test fixture, a temp file).
        JsonNode? root = JsonNode.Parse(json);
        if (root is JsonObject obj)
        {
            if (obj.ContainsKey("Entities") || obj.ContainsKey("Lighting"))
            {
                return "level";
            }

            if (obj.ContainsKey("BaseColorFactor"))
            {
                return "material";
            }

            if (obj.ContainsKey("Physics") && obj.ContainsKey("Rendering"))
            {
                return "settings";
            }
        }

        return "unknown";
    }

    /// <summary>
    /// Recursive semantic comparison. Records every difference rather than stopping at the
    /// first: when the schema shifts, seeing all of the fallout at once is what makes the fix
    /// obvious.
    /// </summary>
    private static void Compare(JsonNode? before, JsonNode? after, string path, List<string> into)
    {
        if (before is null || after is null)
        {
            if (before is not null || after is not null)
            {
                into.Add($"{path}: {Describe(before)} != {Describe(after)}");
            }

            return;
        }

        switch (before)
        {
            case JsonObject beforeObject when after is JsonObject afterObject:
            {
                foreach (var pair in beforeObject)
                {
                    if (!afterObject.ContainsKey(pair.Key))
                    {
                        // The engine's reader ignored this key — almost always a typo or a
                        // field the Python side invented.
                        into.Add($"{path}.{pair.Key}: present in the Python output, dropped by the engine reader");
                        continue;
                    }

                    Compare(pair.Value, afterObject[pair.Key], $"{path}.{pair.Key}", into);
                }

                foreach (var pair in afterObject)
                {
                    if (!beforeObject.ContainsKey(pair.Key))
                    {
                        into.Add($"{path}.{pair.Key}: required by the contract, missing from the Python output");
                    }
                }

                break;
            }

            case JsonArray beforeArray when after is JsonArray afterArray:
            {
                if (beforeArray.Count != afterArray.Count)
                {
                    into.Add($"{path}: array length {beforeArray.Count} != {afterArray.Count}");
                    break;
                }

                for (int i = 0; i < beforeArray.Count; i++)
                {
                    Compare(beforeArray[i], afterArray[i], $"{path}[{i}]", into);
                }

                break;
            }

            case JsonValue beforeValue when after is JsonValue afterValue:
            {
                if (!ValuesEqual(beforeValue, afterValue))
                {
                    into.Add($"{path}: {beforeValue.ToJsonString()} != {afterValue.ToJsonString()}");
                }

                break;
            }

            default:
                into.Add($"{path}: type mismatch ({before.GetType().Name} vs {after.GetType().Name})");
                break;
        }
    }

    private static bool ValuesEqual(JsonValue left, JsonValue right)
    {
        // Numbers compare at float32 precision — the precision the contract actually carries.
        // Comparing the textual form would fail on "5" vs "5.0", which is exactly the kind of
        // difference the value-based contract permits.
        if (left.TryGetValue(out double leftNumber) && right.TryGetValue(out double rightNumber))
        {
            return (float)leftNumber == (float)rightNumber;
        }

        if (left.TryGetValue(out bool leftBool) && right.TryGetValue(out bool rightBool))
        {
            return leftBool == rightBool;
        }

        if (left.TryGetValue(out string? leftString) && right.TryGetValue(out string? rightString))
        {
            return string.Equals(leftString, rightString, StringComparison.Ordinal);
        }

        return string.Equals(left.ToJsonString(), right.ToJsonString(), StringComparison.Ordinal);
    }

    private static string Describe(JsonNode? node) => node?.ToJsonString() ?? "null";
}
