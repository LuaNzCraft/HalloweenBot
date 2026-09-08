BASKET_NORMAL_EMOJI = "🎃"
BASKET_EPIC_EMOJI = "✨"
BASKET_CURSED_EMOJI = "🕷️"


def _decorate_inventory_line(line: str, text) -> str:
    basket_labels = (
        (text("item.cesta_abobora.name"), BASKET_NORMAL_EMOJI),
        (text("item.cesta_doce_epica.name"), BASKET_EPIC_EMOJI),
        (text("item.cesta_doce_amaldicoada.name"), BASKET_CURSED_EMOJI),
    )
    for label, emoji in basket_labels:
        if not label or label not in line:
            continue
        prefix = line.split(label, 1)[0].strip()
        if prefix:
            return line
        if emoji in line:
            return line
        return line.replace(label, f"{emoji} {label}", 1)
    return line


def build_inventory_block(summary: str, i18n=None, locale: str = "pt-BR") -> str:
    def text(key: str, **kwargs) -> str:
        return i18n.t(locale, key, **kwargs) if i18n else key

    summary = str(summary or text("inventory.empty"))
    lines = [_decorate_inventory_line(line.strip(), text) for line in summary.splitlines() if line.strip()]
    if not lines:
        lines = [text("inventory.empty")]
    return f"```md\n{chr(10).join(lines)}\n```"
