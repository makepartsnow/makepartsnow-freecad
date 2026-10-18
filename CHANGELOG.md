# Changelog

## 0.1.2 (2026-10-18)

- Internal update of the shared Make Parts Now CAD core. Nothing changes in what you see or what is sent.

## 0.1.1 (2026-10-18)

- The mouse wheel no longer changes a part's quantity, material or finish when you scroll the panel over it; click a
  box first to change it with the wheel.
- The quote summary counts units, such as "Parts (3 units)".
- Selecting a part that is already in the list refreshes its size.
- Installs from Addon Manager as a custom repository.

## 0.1.0 (2026-10-17, first public beta)

- Quote up to 25 selected PartDesign Bodies, single-solid Part features and closed meshes, each with its own quantity,
  material (SLS PA12 nylon or an FDM plastic) and finish or color: one delivered price before tax for a US ZIP code,
  with printability checks and ship dates. **Order** opens checkout on makepartsnow.com in your browser.
  **Delete my uploaded files** at any time. Anonymous usage counts only if you opt in.
- Exports STEP from a hidden copy, so your document is never saved or changed (meshes as STL in mm). Your name,
  organization and file path are removed from the STEP header before upload.
- Connects only to makepartsnow.com over verified HTTPS.
