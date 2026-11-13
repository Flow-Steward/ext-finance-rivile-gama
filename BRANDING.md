# Branding

Provenance of `icon.png`, the logo Flow Steward shows for this extension.

## Brand

Rivile GAMA is a product of UAB „Rivilė“ (Lithuania). The icon is Rivile's official
mark: the green stacked bars (colour `#00B373`) used across rivile.lt for both
Rivile GAMA and Rivile ERP.

## Source artwork

- Official source: [https://www.rivile.lt/favicon.svg](https://www.rivile.lt/favicon.svg),
  the SVG icon declared by the rivile.lt home page (`<link rel="icon" type="image/svg+xml">`).
- Exact asset: the SVG wraps one embedded raster, a 1000 × 733 RGBA PNG
  (`<image xlink:href="data:image/png;base64,...">`). That PNG is the artwork.
- SHA-256 of `favicon.svg` as downloaded on 2026-11-13:
  `0844b01042b818c280247f2a9cac9d11250f549c37fd03d73f7d49b6c112b184`
- SHA-256 of the embedded 1000 × 733 PNG, decoded from the SVG:
  `54de845037a2349924690c12c57bf8ff2b8f9ede303d4f1ef73f931c27a05d93`

### Other official assets checked (2026-11-13)

- `https://www.rivile.lt/favicon-96x96.png` and `https://www.rivile.lt/favicon.ico`:
  the same mark, smaller than the source above.
- `https://www.rivile.lt/site.webmanifest` lists `web-app-manifest-192x192.png` and
  `web-app-manifest-512x512.png`; both return 404, as does `apple-touch-icon.png`.
- The site header logo is an inline vector lockup of the same bars with the
  "RIVILE" wordmark drawn in white for the dark header. It is a wide lockup,
  not a square mark, and its white wordmark disappears on light cards; cutting
  the bars out of it would be a composition, not the official asset.
- No brand kit, press or logo page is published (`/brand`, `/press`,
  `/spaudai`, `/logotipas`, `/prekes-zenklas`, `/media-kit` all return 404, and
  the home, "Apie Rivile" and Rivile GAMA product pages link none).

The embedded 1000 px PNG is therefore the highest-resolution official mark
available on its own; the icon is a reduction of it, not an enlargement.

## Final icon

- `icon.png`: 128 × 128 RGBA PNG.
- SHA-256: `e97fb20a3ca708c40efd6c31bbab3f5139a3976b76af6d5f5345fb61fc422eb1`
- Processing: the embedded PNG was resized proportionally (Lanczos) from
  1000 × 733 to 112 × 82 and centred on a transparent 128 × 128 canvas at
  offset (8, 23). No colours, shapes or proportions were changed; nothing was
  redrawn, cropped or composed. The SVG itself was not rasterised: its only
  content is the embedded PNG.
- Verified 2026-11-13: repeating that resize from the downloaded source gives
  an alpha mask within 1/255 of `icon.png` and the same bounding box, and the
  only opaque colour is `#00B373` in both. The icon was compared visually with
  the source at full size and enlarged on a light background.

The sibling extension Rivile ERP uses the same source and the same icon.

## Usage terms

Rivile publishes no logo or brand usage guidelines and no separate terms for
Rivile GAMA on rivile.lt (checked 2026-11-13, see the pages above;
`/sistemu-naudojimo-salygos/rivile-gama` returns 404). The closest published
terms are the Rivile ERP system terms of use,
[https://www.rivile.lt/sistemu-naudojimo-salygos/rivile-erp](https://www.rivile.lt/sistemu-naudojimo-salygos/rivile-erp),
section „Intelektinė nuosavybė": UAB „Rivilė“ owns all intellectual property
rights in its systems and documentation, and the same terms prohibit using
Rivile product names or trademarks in hidden text or metadata (15.3.2.6).

"Rivile", "Rivile GAMA" and the mark are trademarks of UAB „Rivilė“. They are used
here only to identify the product this extension connects to. This extension is
not made, endorsed or supported by UAB „Rivilė“.
