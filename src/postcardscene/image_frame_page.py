"""Fixed trusted presentation; only validated fit/count enter generated markup."""

import base64
import hashlib

_SCRIPT = """
const images = [...document.images];
async function present(image) {
    if (image.decode) {
        await image.decode();
    } else if (!image.complete) {
        await new Promise((resolve, reject) => {
            image.onload = resolve;
            image.onerror = reject;
        });
    }
    if (!image.naturalWidth) throw new Error();
}
Promise.all(images.map(present)).then(
    () => fetch('ready', {method: 'POST'}),
    () => fetch('failed', {method: 'POST'})
);
"""


def _hash(text):
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def frame_page(frame):
    style = f"""
html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: black; }}
body {{ display: grid; grid-template-columns: repeat({len(frame.images)}, minmax(0, 1fr)); }}
img {{ width: 100%; height: 100%; min-width: 0; min-height: 0;
       object-fit: {frame.fit}; object-position: center; image-orientation: from-image; }}
"""
    images = "".join(f'<img src="asset/{i}" alt="">' for i in range(len(frame.images)))
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Image frame</title><style>{style}</style></head>"
        f"<body>{images}<script>{_SCRIPT}</script></body></html>"
    )
    csp = (
        "default-src 'none'; img-src 'self'; connect-src 'self'; "
        f"style-src 'sha256-{_hash(style)}'; script-src 'sha256-{_hash(_SCRIPT)}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return page.encode(), csp
