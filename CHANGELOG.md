# Changelog

## 0.1.2 (2026-10-18)

- The shared Make Parts Now CAD core is now 0.1.4: comments only, no change in what the workbench does.

## 0.1.1 (2026-10-18)

- The mouse wheel no longer changes a part's quantity, material or finish when you scroll the panel over it; click
  a box first to change it with the wheel.
- The shared Make Parts Now CAD core is now 0.1.3 (`7aff490`), the version the Fusion add-in 1.0.2 uses. For the
  workbench: the quote summary counts units ("Parts (3 units)"), and re-selecting a part already in the list
  refreshes it. Everything else reads and works as before.

## 0.1.0 (2026-10-17, first public beta)

- First version. Quote the selected PartDesign Bodies, single-solid Part features and closed meshes (up to 25) with
  their own quantity, material (PA12 SLS or an offered FDM plastic) and finish or color; one delivered price before tax
  for a US ZIP code; Order opens checkout on makepartsnow.com in the browser. Delete uploaded files on request.
  Opt-in usage counts.
- Exports STEP from a snapshot in a hidden temporary document (meshes as STL in mm); the source document is never
  saved or changed. The STEP header's author, organization and path are blanked before upload.
- Built on the shared Make Parts Now CAD core 0.1.2 (vendored in `freecad/MakePartsNow/core/`). TLS is always
  verified; when FreeCAD's Python knows no CA certificates (the Linux AppImage) it uses the system's bundle. The
  browser opens only checkout links on makepartsnow.com, and designs are uploaded only to makepartsnow.com.
- Talks to `https://makepartsnow.com/mcp/cad?client=freecad` with the User-Agent `MakePartsNow-FreeCAD/0.1.0`.
