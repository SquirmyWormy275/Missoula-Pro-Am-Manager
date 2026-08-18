# Vendored race-day assets

These files are checked in so the core operator UI remains usable when the
venue has no internet connection. Templates must reference these local files,
not public CDNs.

| Package | Version | Source archive SHA-256 | License |
| --- | --- | --- | --- |
| Bootstrap | 5.3.2 | `6eeae8894f91f0a557f1bbe0ad8eb104bba7a4d8fb1164e37f3113614ebb6c7f` | MIT |
| Bootstrap Icons | 1.11.1 | `710d6dbfb3397bbc29e004aa62b71aa532192e779c2451cf3de0e93f81fd9a0c` | MIT |
| SortableJS | 1.15.2 | `3aad0dbcf0e86c2a6a9e01ec2d9f6e60f97a589dbb88262f66ccd7bb0d574364` | MIT |

The source archives were obtained with `npm pack` at the exact versions above.
Each package's unmodified `LICENSE` file is stored beside its runtime assets.
`manifest.json` binds every served byte to its package, version, license, and
SHA-256 digest. After an intentional upgrade, update the package directory,
license, manifest, template paths, and offline regression test together.

Google-hosted fonts are intentionally not vendored. The UI and print layouts
use their existing system-font fallback stacks when offline.
