# Branding artwork

The integration uses a Tesla-inspired red electric-car, sun and lightning emblem on a transparent background. It is an original mark without the Tesla wordmark or T symbol.

Created and edited with the built-in imagegen tool. Background extraction used a clean white intermediate followed by a transparent PNG edit, including the windshield, headlights, grille and lightning cutouts. The final image was resized with macOS `sips`, preserving alpha, to 256 × 256 and 512 × 512 pixels, stored respectively as:

- `custom_components/tesla_solar_controller/brand/icon.png`
- `custom_components/tesla_solar_controller/brand/icon@2x.png`

The packaged icons were visually checked against white and dark backgrounds.

## Original generation prompt

Create a production app icon for Tesla Solar Controller. Square 1024x1024. A clean solid red emblem of a front-facing sleek electric car with a simple sun rising over its roof, and one lightning-bolt shape cut through the car hood. Tesla-inspired automotive styling. Minimal flat vector-style shapes, red on a perfectly uniform solid dark charcoal #1A1F26 square background. All negative spaces and windows use exactly the same charcoal color as background, fully opaque image. Smooth crisp shapes and NO texture, NO grunge, NO distressed effect, NO speckles, NO gradients, NO reflections or tiny ornament. No text, letters, wordmark or official Tesla T. Simple enough to be readable at 32 pixels. Centered symmetry. Emblem fills 80 percent of image width and height, generous 10 percent padding. Use only red and charcoal colors with antialiased edges.

## White-intermediate edit prompt

Change only the dark charcoal background of this logo to perfectly pure white (#FFFFFF), including the dark windshield, headlights, grille, lightning cutout, and gaps around the sun. Keep the red shapes exactly unchanged in design and proportions. Flat red logo on clean pure white. Square image with the same padding.

## Final transparency edit prompt

Make every white area in this red logo completely transparent. Preserve the red car, sun and rays exactly. This includes the white windshield, headlights, grille, lightning-bolt shape and space around the sun. Keep smooth clean edges. Transparent PNG cutout.
