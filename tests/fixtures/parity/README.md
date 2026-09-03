# Canonical parity corpus

A copy of `ParadiseEngine/src/Paradise.Assets.Documents.Test/Fixtures/parity/`. Every file was
written by the C# `CanonicalTomlWriter` (the `.prefab` through `PrefabDocumentSerializer`) and is
a FIXED POINT of read → write on both sides of the cross-language contract: `ParityCorpusTests`
re-reads and re-emits each file there, `tests/unit/test_parity_corpus.py` does the same here
(ParadiseEngine#209, #29).

Never edit by hand: a hand edit that still parses would make the test pin a form the writer does
not produce. Regenerate in the engine repo when the writing spec changes, then copy.
