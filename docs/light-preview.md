# Light previews from the component schema

A game can declare a native viewport lamp without giving Blender ownership of its light values.
Its archetype component uses `[AuthorLightPreview(HostLightType.Point)]`, `Spot` or `Directional`.
Properties on any of the object's components use `[AuthorLightField(LightPreviewField.Color)]`,
`Intensity`, `Range`, `Size`, `OuterDegrees`, `InnerDegrees`, `Shadows` or `Direction`.

Build the game's launcher to publish `previewLight` on its component schema and `lightField` on
its property schemas. No game names or GUIDs are built into the addon. Nested object properties are
supported; light parameters inside arrays are refused because no row owns the single lamp value.
An object without a light archetype receives no preview.

Open the prefab, select its document object and edit **Components**. Use rendered viewport shading
with scene lights enabled. Move or rotate the document object to move or aim its lamp. For a spot,
document −Z is forward. Parent placement and nonuniform scale are accounted for; the lamp's world
frame has unit scale, so authored range and radius remain metres.

The child light is unselectable and carries no document GUID. It updates on component edits,
transform changes, load and undo; reload removes its previous instance. Saving reads only the
canonical component payload and edit overlay. Editing the native light never changes the document.
This preserves the rule that the `.blend` is a cache and the prefab owns light values.

Colours use the engine's `{r,g,b,a}` object and are decoded from sRGB for the native lamp. The colour
widget reads and writes that same object shape. Point and spot intensity maps to 100 watts per unit;
sun intensity remains native irradiance. Full spot angles are degrees; Blender's blend is
`1 − inner / outer`. Missing shadows means disabled.

The Blender viewport and game renderer can differ in materials, attenuation and shadow filtering.
`AttenuationExponent`, `ShadowStrength` and `Specular` metadata are preserved but have no equivalent
native projection here. Use **Play** to check the game's exact output. Values outside Blender's
limits are clamped for preview only; invalid canonical data remains the game's validator's concern.

Run `tests/integration/test_light_preview.py` in Blender for transform, colour edit, cone, shadow,
round-trip and cleanup checks. `tests/unit/test_light_preview.py` covers schema binding, nested
fields, defaults and colour payloads without Blender.
