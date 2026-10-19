"""Native screenshot clips with physical-pixel size limits."""

from nodriver import cdp


async def screenshot_clip(tab, backend_id, full_page, max_width, max_height):
    result, error = await tab.send(
        cdp.runtime.evaluate(
            "({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight,dpr:devicePixelRatio,"
            "fullWidth:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),"
            "fullHeight:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0)})",
            return_by_value=True,
        )
    )
    if error:
        raise ValueError("Cannot determine screenshot dimensions.")
    dims = result.value
    if backend_id is not None:
        model = await tab.send(
            cdp.dom.get_box_model(backend_node_id=cdp.dom.BackendNodeId(backend_id))
        )
        xs, ys = model.content[::2], model.content[1::2]
        x, y = min(xs) + dims["x"], min(ys) + dims["y"]
        width, height = max(xs) - min(xs), max(ys) - min(ys)
    elif full_page:
        x, y, width, height = 0, 0, dims["fullWidth"], dims["fullHeight"]
    else:
        x, y, width, height = dims["x"], dims["y"], dims["width"], dims["height"]
    if width <= 0 or height <= 0:
        raise ValueError("Screenshot target has no visible dimensions.")
    scale = min(
        1,
        max_width / (width * dims["dpr"]) if max_width else 1,
        max_height / (height * dims["dpr"]) if max_height else 1,
    )
    # Keep at least one physical pixel on the shorter edge of very thin crops.
    if min(width, height) * dims["dpr"] * scale < 1:
        raise ValueError(
            "Screenshot size limits produce a subpixel image; increase the limits."
        )
    return cdp.page.Viewport(x, y, width, height, scale)
