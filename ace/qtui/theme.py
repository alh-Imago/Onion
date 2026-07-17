"""
theme.py — Light/dark QSS stylesheets for the Onion Qt UI.

Colour tokens are the exact same values used in ace/webui.py's embedded
CSS, so the Qt frontend visually matches the web frontend as closely as
Qt's styling model allows, per the original brief. Button "variants"
(primary/ghost/danger) are done via a dynamic Qt property (`class`) since
QSS doesn't support CSS-style class selectors natively -- set it with
`button.setProperty("class", "primary")` before the stylesheet is applied
(or call button.style().unpolish/polish() if changing it after the fact).
"""

LIGHT = {
    "bg": "#EEF1F0", "surface": "#FFFFFF", "text": "#1B2422", "muted": "#5B6B67",
    "accent": "#2B6E63", "accent2": "#1F5147", "border": "#D3DAD8",
    "badge_enc": "#8A4B9E", "badge_bg": "#E9F1EF",
}

DARK = {
    "bg": "#12181A", "surface": "#1B2325", "text": "#E4EAE8", "muted": "#8FA19C",
    "accent": "#4FBFAD", "accent2": "#6FD6C4", "border": "#2B3638",
    "badge_enc": "#B98BCB", "badge_bg": "#223330",
}


def build_stylesheet(tokens: dict) -> str:
    t = tokens
    return f"""
    QMainWindow, QDialog {{
        background: {t['bg']};
    }}
    QWidget {{
        color: {t['text']};
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        font-size: 13px;
    }}
    QLabel[class="mono"] {{
        font-family: "IBM Plex Mono", "Consolas", monospace;
    }}
    QLabel[class="muted"] {{
        color: {t['muted']};
        font-size: 11px;
    }}
    QLabel[class="heading"] {{
        font-size: 18px;
        font-weight: 600;
    }}
    QFrame[class="card"] {{
        background: {t['surface']};
        border: 1px solid {t['border']};
        border-left: 4px solid {t['accent']};
        border-radius: 8px;
    }}
    QFrame[class="panel"] {{
        background: {t['surface']};
        border: 1px solid {t['border']};
        border-radius: 10px;
    }}
    QLineEdit {{
        background: {t['bg']};
        border: 1px solid {t['border']};
        border-radius: 6px;
        padding: 6px 8px;
        color: {t['text']};
        font-family: "IBM Plex Mono", "Consolas", monospace;
    }}
    QLineEdit:focus {{
        border: 1px solid {t['accent']};
    }}
    QCheckBox {{
        spacing: 8px;
    }}
    QPushButton {{
        border-radius: 7px;
        padding: 7px 14px;
        font-weight: 500;
        border: 1px solid {t['border']};
        background: transparent;
        color: {t['text']};
    }}
    QPushButton:hover {{
        border: 1px solid {t['accent']};
        color: {t['accent']};
    }}
    QPushButton[class="primary"] {{
        background: {t['accent']};
        color: white;
        border: 1px solid {t['accent']};
    }}
    QPushButton[class="primary"]:hover {{
        background: {t['accent2']};
        border: 1px solid {t['accent2']};
        color: white;
    }}
    QPushButton[class="danger"] {{
        border: 1px solid {t['badge_enc']};
        color: {t['badge_enc']};
        background: transparent;
    }}
    QPushButton[class="danger"]:hover {{
        background: {t['badge_bg']};
    }}
    QPushButton[class="danger-armed"] {{
        background: {t['badge_enc']};
        color: white;
        border: 1px solid {t['badge_enc']};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QWidget#resultsContainer, QScrollArea > QWidget > QWidget {{
        background: {t['bg']};
    }}
    QLabel[class="badge"] {{
        background: {t['badge_bg']};
        color: {t['accent2']};
        border-radius: 9px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 500;
    }}
    QLabel[class="badge-enc"] {{
        background: {t['badge_bg']};
        color: {t['badge_enc']};
        border-radius: 9px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 500;
    }}
    QLabel[class="warning"] {{
        background: {t['badge_bg']};
        color: {t['badge_enc']};
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 11px;
    }}
    """


def apply_theme(app, dark: bool = False):
    """Apply the light or dark stylesheet to the whole QApplication."""
    app.setStyleSheet(build_stylesheet(DARK if dark else LIGHT))
