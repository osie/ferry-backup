# Branding the console

An operator can put their own name, mark and colour on Ferry's console, the way
Horizon takes `SITE_BRANDING`, a logo and a theme. It changes what the console
looks like and nothing else: the API, the CLI, the catalog entry and the Horizon
panel label are untouched.

| `manager.yaml` key           | What it changes                                                                 |
| ---------------------------- | ------------------------------------------------------------------------------- |
| `console.product_name`       | The name in the sidebar, on the sign-in card, in the browser tab, and wherever the console's own sentences say "Ferry". At most 64 characters. |
| `console.logo`               | The square mark beside the name, in place of the lettered tile. Shown at 24px in the sidebar and 28px on the sign-in card. |
| `console.logo_dark`          | The mark on the dark theme. Needs `console.logo`.                               |
| `console.logo_only`          | Show the logo alone, up to 32px tall and 160px wide in the sidebar and 48px on the sign-in card, with no name beside it. The name still titles the tab, is the logo's alt text, and stands in the console's sentences. Needs `console.logo`. |
| `console.logo_align`         | `left` (the default) or `center`: where the logo alone sits in the sidebar and on the sign-in card. Needs `console.logo_only`. |
| `console.logo_height`        | The logo alone's height in the sidebar, 12 to 96 CSS pixels; its width follows its own proportions. The header grows to fit. Needs `console.logo_only`. |
| `console.logo_width`         | Its width, 12 to 208 CSS pixels; its height follows. With both set, the logo fits inside that box. The sign-in card draws either at 1.5×, and the collapsed sidebar keeps its 24px square. Needs `console.logo_only`. |
| `console.favicon`            | The browser tab icon. Left out, `console.logo` is used.                         |
| `console.primary_color`      | The brand colour: primary buttons, checkboxes, progress bars, the lettered tile and the focus ring. `#rrggbb` or `#rgb`. |
| `console.primary_color_dark` | The brand colour on the dark theme. Needs `console.primary_color`; left out, it is used on both. |
| `console.stylesheet`         | CSS loaded after the console's own, for anything the keys above do not reach.   |

```yaml
console:
  product_name: Acme Cloud Backup
  logo: /etc/ferry/console/logo.svg
  logo_dark: /etc/ferry/console/logo-dark.svg
  primary_color: "#0b5cad"
  primary_color_dark: "#6aa8ff"
```

Text on a brand-coloured button is picked for you: near-white or near-black,
whichever contrasts more with the colour you gave. A pale yellow gets dark text
and a navy gets light text, so the console's 4.5:1 floor holds whatever the
colour is.

## What the manager checks

Each file is read once, at start, and refused if it is missing, empty, not a
regular file, or larger than 1 MiB (256 KiB for the stylesheet). An image's type
is read from its bytes, not its name: svg, png, webp, gif, jpeg and ico are
accepted and anything else is refused with what it actually is. A refusal stops
the manager from starting, with the key and the path in the message, the same as
any other configuration mistake.

The manager serves them under the console's own mount, at relative paths, so
they work behind whatever prefix a proxy puts the console under:

| Path                  | Cache                                   |
| --------------------- | --------------------------------------- |
| `branding.css`        | a day, and the page asks for it by content hash, so a change is picked up on the next page load |
| `branding/logo`, `branding/logo-dark`, `branding/favicon` | the same |
| `index.html`          | `no-cache`, because it carries the hashes |

An SVG is served with a sandboxing `Content-Security-Policy`, so a script inside
one cannot run in the console's origin even when the file is opened directly.

Changing a file or a key means restarting the manager. Every deployer below
restarts it when it rewrites the configuration.

## The stylesheet

The console is drawn from CSS custom properties, as `H S% L%` triplets, set
on `:root` for the light theme and on `[data-theme='dark']` for the dark one.
Those names are the stable surface: `--background`, `--foreground`, `--card`,
`--popover`, `--primary`, `--secondary`, `--muted`, `--accent`, `--destructive`,
`--success`, `--warning`, each with its `-foreground`, and `--border`, `--input`,
`--ring` and `--radius`.

```css
:root { --radius: 0.25rem; --background: 210 20% 98%; }
[data-theme='dark'] { --background: 215 28% 10%; --card: 215 28% 13%; }
```

Anything beyond the tokens, such as a class name or an element selector, is
not a contract and can break on any upgrade. Contrast is also yours to keep
once you change a background.

## kolla-ansible

Put the files in `/etc/kolla/config/ferry/console/`, then name them in
`globals.yml` by file name:

```yaml
ferry_console_product_name: "Acme Cloud Backup"
ferry_console_logo: "logo.svg"
ferry_console_logo_dark: "logo-dark.svg"
ferry_console_logo_only: false
ferry_console_logo_align: ""
ferry_console_logo_height: 0
ferry_console_logo_width: 0
ferry_console_favicon: "favicon.png"
ferry_console_primary_color: "#0b5cad"
ferry_console_primary_color_dark: "#6aa8ff"
ferry_console_stylesheet: ""
```

`kolla-ansible -i <inventory> reconfigure --tags ferry` copies the named files to
each manager and restarts it. A file that is named but missing fails the copy, before any
manager restarts.

## Helm

The chart reads the files from a ConfigMap you create, which carries binary
images as they are:

```sh
kubectl -n openstack create configmap ferry-console \
  --from-file=logo.svg --from-file=logo-dark.svg --from-file=favicon.png
```

```yaml
console:
  productName: Acme Cloud Backup
  primaryColor: "#0b5cad"
  primaryColorDark: "#6aa8ff"
  filesConfigMap: ferry-console
  logo: logo.svg
  logoDark: logo-dark.svg
  logoOnly: false
  logoAlign: ""
  logoHeight: 0
  logoWidth: 0
  favicon: favicon.png
```

Naming a file without `filesConfigMap` fails the render. The chart does not own
that ConfigMap, so after changing it run
`kubectl -n openstack rollout restart deploy/<release>-ferry-manager`.

## Juju

Everything is an option. The files go in base64, so a PNG survives the trip:

```sh
juju config ferry console-product-name="Acme Cloud Backup" \
  console-primary-color="#0b5cad" console-primary-color-dark="#6aa8ff" \
  console-logo="$(base64 -w0 logo.svg)" console-logo-dark="$(base64 -w0 logo-dark.svg)"
```

`console-favicon` and `console-stylesheet` work the same way.
`console-logo-only=true` shows the logo without the name, and `console-logo-align`,
`console-logo-height` and `console-logo-width` place and size it. Setting an option
re-renders the configuration and restarts the manager, and setting one back to
empty removes the file. A value that is not base64 blocks the unit and names the
option.

They are options rather than resources because these charms are deployed
from a local file, and Juju refuses a local-charm refresh that declares a new
resource unless every one is supplied on the command line.
