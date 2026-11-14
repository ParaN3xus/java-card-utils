#import "@preview/tiaoma:0.4.0"
#import "lib.typ": *

#let default = "ffffffffffffffffffffffffffffffff"

#let text = (
  "export GP_KEY_ENC="
    + sys.inputs.at("ENC", default: default)
    + "\n"
    + "export GP_KEY_MAC="
    + sys.inputs.at("MAC", default: default)
    + "\n"
    + "export GP_KEY_DEK="
    + sys.inputs.at("DEK", default: default)
    + "\n"
)


#grid(columns: 100%, rows: (1fr, 1fr), row-gutter: 1cm)[
  = GP Key Backup
  - Date: #datetime.today().display()

  - Keys:
    #raw(text, block: true)
][
  #fit-height(tiaoma.qrcode(text))
]
