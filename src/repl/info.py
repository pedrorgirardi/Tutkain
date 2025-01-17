import html
import inspect
import os
import re
import sublime
import tempfile

from urllib.parse import urlparse
from zipfile import ZipFile

from ...api import edn


def rename(view, name):
    if view is not None:
        if view.is_loading():
            sublime.set_timeout(lambda: rename(view, name), 100)
        else:
            view.set_name(name)


def goto(window, location):
    if location:
        resource = location["resource"]
        line = location["line"] + 1
        column = location["column"] + 1

        if not resource.scheme or resource.scheme == "file" and resource.path:
            view = window.open_file(f"{resource.path}:{line}:{column}", flags=sublime.ENCODED_POSITION)
        elif resource.scheme == "jar" and "!" in resource.path:
            parts = resource.path.split("!")
            jar_url = urlparse(parts[0])
            # If the path after the ! starts with a forward slash, strip it. ZipFile can't
            # find the file inside the archive otherwise.
            path = parts[1][1:] if parts[1].startswith("/") else parts[1]
            archive = ZipFile(jar_url.path, "r")
            source_file = archive.read(path)
            view_name = jar_url.path + "!/" + path
            descriptor, path = tempfile.mkstemp()

            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write(source_file.decode())

                # TODO: Add setting?
                flags = sublime.ENCODED_POSITION | sublime.ADD_TO_SELECTION | sublime.SEMI_TRANSIENT | sublime.CLEAR_TO_RIGHT
                view = window.open_file(f"{path}:{line}:{column}", flags=flags)
                view.assign_syntax("Clojure (Tutkain).sublime-syntax")
                view.set_scratch(True)
                view.set_read_only(True)
                rename(view, view_name)
            finally:
                os.remove(path)


def parse_location(info):
    if info and (file := info.get(edn.Keyword("file"))):
        return {
            "resource": urlparse(file),
            "line": int(info.get(edn.Keyword("line"), "1")) - 1,
            "column": int(info.get(edn.Keyword("column"), "1")) - 1,
        }


def htmlify(text):
    if text:
        return re.sub(r"\n", "<br/>", inspect.cleandoc(html.escape(text)))
    else:
        return ""


def show_popup(view, point, response):
    if edn.Keyword("info") in response:
        info = response[edn.Keyword("info")]

        if info:
            file = info.get(edn.Keyword("file"), "")
            location = parse_location(info)
            ns = info.get(edn.Keyword("ns"), "")
            name = info.get(edn.Keyword("name"), edn.Symbol(""))
            arglists = info.get(edn.Keyword("arglists"), "")
            spec = info.get(edn.Keyword("spec"), "")
            fnspec = info.get(edn.Keyword("fnspec"), {})
            doc = info.get(edn.Keyword("doc"), "")

            if ns and name and file:
                name = f"""
                <p class="name">
                    <a href="{file}">{ns}/{name}</a>
                </p>
                """

            elif name and file:
                name = f"""
                <p class="name">
                    <a href="{file}">{name}</a>
                </p>
                """
            elif name:
                name = f"""
                <p class="name">{name}</p>
                """
            else:
                name = ""

            content = f"""
                <body id="tutkain-lookup">
                    <style>
                        #tutkain-lookup {{
                            font-size: .9rem;
                            padding: 0;
                            margin: 0;
                        }}

                        a {{
                            text-decoration: none;
                        }}

                        p {{
                            margin: 0;
                            padding: .25rem .5rem;
                        }}

                        .name, .arglists, .spec, .fnspec {{
                            border-bottom: 1px solid color(var(--foreground) alpha(0.05));
                        }}

                        .arglists, .spec, .fnspec {{
                            color: color(var(--foreground) alpha(0.5));
                        }}

                        .fnspec-key {{
                            color: color(var(--foreground) alpha(0.75));
                        }}
                    </style>
                    {name}"""

            if arglists:
                content += """<p class="arglists">"""

                for arglist in arglists:
                    content += f"""<code>{htmlify(arglist)}</code> """

                content += "</p>"

            if spec:
                content += f"""<p class="spec"><code>{htmlify(spec)}</code></p>"""

            if fnspec:
                content += """<p class="fnspec">"""

                for k in [edn.Keyword("args"), edn.Keyword("ret"), edn.Keyword("fn")]:
                    if k in fnspec:
                        content += f""":<span class="fnspec-key">{k.name}</span> {htmlify(fnspec[k])}<br/>"""

                content += """</p>"""

            if doc:
                doc = re.sub(r"\s", "&nbsp;", htmlify(doc))
                content += f"""<p class="doc">{doc}</p>"""

            content += "</body>"

            view.show_popup(
                content,
                location=point,
                max_width=1024,
                on_navigate=lambda href: goto(view.window(), location), flags=sublime.COOPERATE_WITH_AUTO_COMPLETE
            )
