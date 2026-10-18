# Security

Please report a vulnerability in the workbench, or in how it talks to makepartsnow.com, by email to
nathan@makepartsnow.com with "security" in the subject. Don't open a public issue. We answer within 3 business days.

What the workbench does to stay small and safe:

- It talks only to makepartsnow.com over HTTPS with certificate checks (plain HTTP only to localhost, for development).
- It opens in the browser only https links on makepartsnow.com (or the configured development server).
- It shows server text as plain text, never rich text or HTML.
- It has no dependencies beyond FreeCAD and Python's standard library, no network or thread at FreeCAD start, and
  never runs downloaded code.
- It never sees payment details (checkout is on makepartsnow.com through Stripe) and never stores the files it
  uploads. Delete tokens for your uploads stay in memory only.

Supported versions: the latest release.
