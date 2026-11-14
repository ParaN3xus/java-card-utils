#let fit-height(body) = {
  set align(center)
  block(height: 1fr, layout(size => {
    let body-size = measure(body).height
    scale(body, size.height / body-size * 100%, reflow: true)
  }))
}
