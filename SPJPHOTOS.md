# SPJ Photos Project Summary

Workspace: `/home/tfuller/git/spjphotos`

Goal: create a plainly formatted but polished single-page photo gallery for Jane Fuller and Siguiendo los Pasos de Jesus (SPJ), suitable to publish online and share as supporting material for a 2026 CNN Heroes nomination.

## Main Output

- `index.html`
  - Single-file HTML/CSS/JS page.
  - References images from the existing `pictures/` folder.
  - Uses a hero photo, nomination-oriented intro, sticky category navigation, 20 grouped photo sections, captions, public context links, and a lightbox for reviewing images.
  - The page should be published with `index.html` beside the `pictures/` directory so all relative image paths resolve.

## Photo Coverage

The page groups the supplied images into the 20 requested themes:

1. Jane Fuller with the families she serves
2. Harsh desert and isolation
3. Pallet/cardboard/dirt-floor homes
4. SPJ homes with families
5. Workers building homes
6. San Mateo Church
7. Community Center exterior and services
8. Mercado exterior
9. Activity Center/Gymnasium
10. SPJ Library
11. Park for children
12. Park for teens and adults
13. Community van and school transportation
14. Inside the Community Center
15. People in the Mercado
16. People in the Gymnasium
17. People in the Library
18. San Mateo Clinic and medical care
19. Clothing distribution in the Gymnasium
20. Food distribution and winter wood

## Research Context Used

The page includes public context links for:

- CNN Heroes nomination page: `https://www.cnn.com/world/heroes/nominations`
- SPJ official site: `https://www.spjinc.org/`
- SPJ public giving profile
- KVIA article on SPJ home building
- El Comercio article on the SPJ clinic/Jane Fuller

The copy stays conservative and uses the supplied photo evidence as the primary story.

## Verification Performed

- Confirmed `index.html` references 20 sections and 76 photo entries.
- Confirmed all referenced `pictures/...` files exist.
- Rendered in Google Chrome headless at desktop size.
- Rendered an emulated 390px mobile viewport through Chrome DevTools Protocol.
- Fixed mobile horizontal clipping by changing shared container sizing and preserving hero horizontal padding.

## HEIC Conversion

The two clothing distribution files were supplied as difficult-to-read `.HEIC` files:

- `pictures/19a clothing distributed.HEIC`
- `pictures/19b clothing distributed.HEIC`

They have been converted to browser-friendly JPEG files and section 19 now renders them as normal gallery photos:

- `pictures/19a clothing distributed.jpg`
- `pictures/19b clothing distributed.jpg`

## Current Git Notes

- `index.html` exists and is the completed gallery page.
- `codex.txt` is present as an untracked file and contains only an ID-like string.
- This summary file is `SPJPHOTOS.md`.
