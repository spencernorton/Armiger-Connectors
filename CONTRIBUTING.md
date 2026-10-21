# Contributing to Armiger-Connectors

Thank you for contributing to `Armiger-Connectors`.

## Ground Rules

1. **Stock Upstream Compatibility:**
   - Every connector in this repository must work against **stock, unmodified upstream** releases (`bitmagnet-io/bitmagnet` and `librats/rats-search`) using only their documented REST, WebSocket, GraphQL, or database interfaces.
2. **Simplicity & Efficiency:**
   - Keep connectors self-contained, dependency-light (Python standard library + `armiger`), and batch-oriented (`classify_batch`, grouped GraphQL mutations, and docid-indexed Manticore updates).
3. **Copyright & Legal Hygiene:**
   - Never include copyrighted media titles, scene/release-group names, or private-tracker identifiers in code, comments, or test fixtures.
   - Use Creative Commons Attribution (CC-BY) open movies (*Sintel*, *Tears of Steel*, *Big Buck Bunny*, *Cosmos Laundromat*, *Caminandes*, *Spring*, *Coffee Run*, *Charge*, *Wing It!*, *Elephants Dream*), CC-BY webcomics (*Pepper&Carrot*), CC0 recordings (*Open Goldberg Variations*), or public-domain literature (*LibriVox*, *Project Gutenberg*) in all tests and examples.
4. **Developer Certificate of Origin (DCO):**
   - Sign off every commit with `git commit -s` (`Signed-off-by: Name <email>`), certifying the [Developer Certificate of Origin v1.1](https://developercertificate.org/).

## Running the Test Suite

```bash
pytest -v
```
