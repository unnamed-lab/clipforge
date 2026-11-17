# Bundled font notices

All fonts in this directory are distributed under the SIL Open Font License 1.1
(see `OFL.txt`). They are bundled so caption measurement and burned-in captions
are reproducible offline and inside the worker container.

| File | Family | Copyright |
| --- | --- | --- |
| `Montserrat-Bold.ttf`, `Montserrat-Black.ttf` | Montserrat | Copyright 2011 The Montserrat Project Authors (https://github.com/JulietaUla/Montserrat) |
| `BebasNeue-Regular.ttf` | Bebas Neue | Copyright 2010 The Bebas Neue Project Authors |
| `Anton-Regular.ttf` | Anton | Copyright 2020 The Anton Project Authors |
| `ArchivoBlack-Regular.ttf` | Archivo Black | Copyright 2017 The Archivo Black Project Authors |
| `Lato-Bold.ttf` | Lato | Copyright 2010 The Lato Project Authors |
| `Inter-Variable.ttf` | Inter | Copyright 2016 The Inter Project Authors |

## Substitutions

The product exposes familiar names that cannot be redistributed:

- **SF Pro Display** → Inter
- **Helvetica** and **Arial** → Lato
- **The Bold Font** → Archivo Black
- **Impact** → Anton

The renderer stores the requested name, the actual bundled family, and whether a
substitution occurred, so the UI and review report never imply that a
proprietary font was shipped.
