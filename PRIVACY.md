# Privacy: Make Parts Now for FreeCAD

What the workbench itself sends and stores. The site's [privacy policy](https://makepartsnow.com/legal/privacy) and
[terms](https://makepartsnow.com/legal/terms) cover what Make Parts Now does with the data.

## When anything is sent

Nothing is sent until you have accepted the consent text (shown once per install) and clicked **Get instant quote**.
Opening the panel fetches the current options (materials, finishes and colors, limits, any promotion code) from
makepartsnow.com; that request carries no part data. Selecting parts sends nothing.

## What is sent for a quote

- One file per selected part, named after the part: STEP for a solid, STL (in mm) for a mesh, or STL for a solid only
  if STEP export failed and you agreed. The file is written from a copy of the part in a hidden temporary document;
  your document is not saved or changed. The author, organization and file path in the STEP header are blanked
  before upload.
- The quantity, material and finish or color you chose for each part, and the ZIP code the parts ship to.
- The workbench's name and version, in the `User-Agent` header.

Nothing else from your document is sent: not the `.FCStd` file, other objects, macros or screenshots. You don't sign
in. The data goes only to makepartsnow.com.

## Your files

Uploaded files are stored encrypted and deleted automatically under the privacy policy's retention periods; files on
an order are kept with the order. To delete them sooner, click **Delete my uploaded files** in the panel (it covers
the files uploaded since the panel opened) or email nathan@makepartsnow.com.

## Paying

The workbench is free. You order and pay on makepartsnow.com through Stripe Checkout, in your browser. The workbench
never sees card or payment details.

## Usage counts (off unless you opt in)

If you tick **Send anonymous usage counts**, the workbench sends counts of a fixed list of events (such as
`quote_shown`) with its version, as `{client: "freecad", version, events: {name: count}}`. Never file names, part
data, ids or personal data. A failed send is dropped.

## Stored on your computer

`<FreeCAD user data>/MakePartsNow/freecad-workbench.json` (the folder that holds `Mod`; in FreeCAD, `App.getUserAppDataDir()`): whether and
when you accepted the consent text, your usage-count choice, the last ZIP code, and an optional developer server
address. Exported files go to a private temporary folder and are deleted after the upload, whether it worked or not.
