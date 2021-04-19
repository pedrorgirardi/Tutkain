from concurrent.futures import TimeoutError
from inspect import cleandoc
import json
import os
import sublime
from threading import Thread

from sublime_plugin import (
    EventListener,
    ListInputHandler,
    TextCommand,
    TextInputHandler,
    ViewEventListener,
    WindowCommand,
)

from .src import selectors
from .src import sexp
from .src import state
from .src import forms
from .src import indent
from .src import inline
from .src import paredit
from .src import namespace
from .src import test
from .src.repl.client import Client
from .src.repl import info
from .src.repl import history
from .src.repl import tap
from .src.repl import ports
from .src.repl import printer
from .src.repl import views


from .src.log import start_logging, stop_logging


from .api import edn


def make_color_scheme(cache_dir):
    """
    Add the tutkain.repl.standard-streams scope into the current color scheme.

    We want stdout/stderr messages in the same REPL output view as evaluation results, but we don't
    want them to be use syntax highlighting. We can use view.add_regions() to add a scope to such
    messages such that they are not highlighted. Unfortunately, it is not possible to use
    view.add_regions() to only set the foreground color of a region. Furthermore, if we set the
    background color of the scope to use exactly the same color as the global background color of
    the color scheme, Sublime Text refuses to apply the scope.

    We therefore have to resort to this awful hack where every time the plugin is loaded or the
    color scheme changes, we generate a new color scheme in the Sublime Text cache directory. That
    color scheme defines the tutkain.repl.stdout scope which has an almost-transparent background
    color, creating the illusion that we're only setting the foreground color of the text.

    Yeah. So, please go give this issue a thumbs-up:

    https://github.com/sublimehq/sublime_text/issues/817
    """
    view = sublime.active_window().active_view()

    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    if view:
        color_scheme = view.settings().get("color_scheme")

        if color_scheme:
            (scheme_name, _) = os.path.splitext(os.path.basename(color_scheme))

            scheme_path = os.path.join(cache_dir, f"{scheme_name}.sublime-color-scheme")

            if not os.path.isfile(scheme_path):
                with open(scheme_path, "w") as scheme_file:
                    scheme_file.write(
                        json.dumps(
                            {
                                "rules": [
                                    {
                                        "name": "Tutkain REPL Standard Output",
                                        "scope": "tutkain.repl.stdout",
                                        "background": "rgba(0, 0, 0, 0.01)",
                                    },
                                    {
                                        "name": "Tutkain REPL Standard Error",
                                        "scope": "tutkain.repl.stderr",
                                        "background": "rgba(0, 0, 0, 0.01)",
                                        "foreground": view.style().get(
                                            "redish", "crimson"
                                        ),
                                    },
                                ]
                            }
                        )
                    )


def settings():
    return sublime.load_settings("Tutkain.sublime-settings")


def plugin_loaded():
    start_logging(settings().get("debug", False))

    preferences = sublime.load_settings("Preferences.sublime-settings")

    cache_dir = os.path.join(sublime.cache_path(), "Tutkain")

    make_color_scheme(cache_dir)
    preferences.add_on_change("Tutkain", lambda: make_color_scheme(cache_dir))


def plugin_unloaded():
    stop_logging()

    for window in sublime.windows():
        window.run_command("tutkain_disconnect")

    view = sublime.active_window().active_view()
    view and inline.clear(view)

    preferences = sublime.load_settings("Preferences.sublime-settings")
    preferences.clear_on_change("Tutkain")


def dialect(view, point):
    if view.match_selector(point, "source.clojure.clojurescript"):
        return edn.Keyword("cljs")
    else:
        return edn.Keyword("clj")


def source_root():
    return os.path.join(
        sublime.packages_path(), "Tutkain", "clojure", "src", "tutkain", "repl", "runtime"
    )


class TutkainClearOutputViewCommand(WindowCommand):
    def clear_view(self, view):
        if view:
            view.set_read_only(False)
            view.run_command("select_all")
            view.run_command("right_delete")
            view.set_read_only(True)
            inline.clear(self.window.active_view())

    def run(self):
        view = state.repl_view(self.window)

        if view:
            self.clear_view(view)

        panel = self.window.find_output_panel(tap.panel_name)
        panel and self.clear_view(panel)


class TutkainEvaluateFormCommand(TextCommand):
    def run(self, edit, scope="outermost", ignore={"comment"}, inline_result=False):
        self.view.window().status_message("tutkain_evaluate_form is deprecated; use tutkain_evaluate instead")
        self.view.run_command("tutkain_evaluate", {"scope": scope, "ignore": ignore, "inline_result": inline_result})


class TutkainEvaluateViewCommand(TextCommand):
    def run(self, edit):
        self.view.window().status_message("tutkain_evaluate_view is deprecated; use tutkain_evaluate instead")
        self.view.run_command("tutkain_evaluate", {"scope": "view"})


class TutkainRunTests(TextCommand):
    def run(self, edit, scope="ns"):
        if scope == "ns":
            client = state.client(self.view.window())
            test.run(self.view, client)
        elif scope == "var":
            region = self.view.sel()[0]
            point = region.begin()
            test_var = test.current(self.view, point)

            if test_var:
                client = state.client(self.view.window())
                test.run(self.view, client, test_vars=[test_var])

    def input(self, args):
        if "scope" in args:
            return None
        else:
            return TestScopeInputHandler()


class TestScopeInputHandler(ListInputHandler):
    def placeholder(self):
        return "Choose test scope"

    def list_items(self):
        return [
            sublime.ListInputItem("Var", "var", details="Run the test defined by the var under the caret.", annotation="<code>deftest</code>"),
            sublime.ListInputItem("Namespace", "ns", details="Run all tests in the current namespace."),
        ]


class TutkainRunTestsInCurrentNamespaceCommand(TextCommand):
    def run(self, edit):
        self.view.window().status_message("tutkain_run_tests_in_current_namespace is deprecated; use tutkain_run_tests instead")
        self.view.run_command("tutkain_run_tests", {"scope": "ns"})


class TutkainRunTestUnderCursorCommand(TextCommand):
    def run(self, edit):
        self.view.window().status_message("tutkain_run_test_under_cursor is deprecated; use tutkain_run_tests instead")
        self.view.run_command("tutkain_run_tests", {"scope": "var"})


class HostInputHandler(TextInputHandler):
    def __init__(self, window):
        self.window = window

    def placeholder(self):
        return "Host"

    def validate(self, text):
        return len(text) > 0

    def initial_text(self):
        return "localhost"

    def next_input(self, args):
        return PortInputHandler(self.window)


class PortInputHandler(TextInputHandler):
    def __init__(self, window):
        self.window = window

    def name(self):
        return "port"

    def placeholder(self):
        return "Port"

    def validate(self, text):
        return text.isdigit()

    def initial_text(self):
        alts = ports.discover(self.window)

        if alts:
            return alts[0][1]
        else:
            return ""


class TutkainEvaluateInputCommand(WindowCommand):
    def run(self):
        self.window.status_message("tutkain_evaluate_input is deprecated; use tutkain_evaluate instead")
        self.window.active_view().run_command("tutkain_evaluate", {"scope": "input"})


class TutkainEvaluateCommand(TextCommand):
    def get_eval_region(self, region, scope="outermost", ignore={}):
        if not region.empty():
            return region
        else:
            if scope == "form":
                return forms.find_adjacent(self.view, region.begin())
            elif scope == "innermost" and (innermost := sexp.innermost(self.view, region.begin(), edge=True)):
                return innermost.extent()
            elif scope == "outermost" and (outermost := sexp.outermost(self.view, region.begin(), ignore=ignore)):
                return outermost.extent()

    def evaluate_view(self, client, code):
        file = self.view.file_name()

        op = {
            edn.Keyword("op"): edn.Keyword("load"),
            edn.Keyword("code"): code,
            edn.Keyword("file"): file,
            edn.Keyword("dialect"): dialect(self.view, 0)
        }

        if ns := namespace.name(self.view):
            client.switch_namespace(ns, dialect(self.view, 0))

        client.backchannel.send(op, handler=client.recvq.put)

    def handler(self, region, client, response, inline_result):
        if inline_result and edn.Keyword("val") in response:
            inline.clear(self.view)
            inline.show(self.view, region.end(), response[edn.Keyword("val")])
        else:
            client.recvq.put(response)

    def evaluate_input(self, client, code):
        client.recvq.put({edn.Keyword("in"): code})
        client.eval(code)
        history.update(self.view.window(), code)

    def noop(*args):
        pass

    def run(self, edit, scope="outermost", code="", ns=None, ignore={"comment"}, inline_result=False):
        assert scope in {"input", "form", "ns", "innermost", "outermost", "view"}

        client = state.client(self.view.window())

        if client is None:
            self.view.window().status_message("ERR: Not connected to a REPL.")
        else:
            if code:
                if ns:
                    ns_before = client.namespace

                    try:
                        client.switch_namespace(ns)
                        client.eval(code)
                    finally:
                        client.switch_namespace(ns_before)
                else:
                    variables = {}

                    for index, region in enumerate(self.view.sel()):
                        if eval_region := self.get_eval_region(region, scope, ignore):
                            variables[str(index)] = self.view.substr(eval_region)

                    code = sublime.expand_variables(code, variables)
                    client.recvq.put({edn.Keyword("in"): code})
                    client.eval(code)
            elif scope == "input":
                view = self.view.window().show_input_panel(
                    "Input: ",
                    history.get(self.view.window()),
                    lambda code: self.evaluate_input(client, code),
                    self.noop,
                    self.noop,
                )

                view.settings().set("tutkain_repl_input_panel", True)
                view.assign_syntax("Clojure (Tutkain).sublime-syntax")
            elif scope == "view":
                eval_region = sublime.Region(0, self.view.size())

                if not eval_region.empty():
                    self.evaluate_view(client, self.view.substr(eval_region))
            elif scope == "ns":
                forms = namespace.forms(self.view)

                for form in forms:
                    code = self.view.substr(form)
                    client.recvq.put({edn.Keyword("in"): code})
                    client.eval(code)
            else:
                for region in self.view.sel():
                    eval_region = self.get_eval_region(region, scope, ignore)
                    code = self.view.substr(eval_region)
                    client.recvq.put({edn.Keyword("in"): code})
                    client.eval(code, lambda response: self.handler(eval_region, client, response, inline_result))

    def input(self, args):
        if any(map(lambda region: not region.empty(), self.view.sel())):
            return None
        if "scope" in args:
            return None
        else:
            return EvaluationScopeInputHandler()


class EvaluationScopeInputHandler(ListInputHandler):
    def placeholder(self):
        return "Choose evaluation scope"

    def list_items(self):
        return [
            sublime.ListInputItem("Adjacent form", "form", details="The form (not necessarily S-expression) adjacent to the caret."),
            sublime.ListInputItem("Innermost S-expression", "innermost", details="The innermost S-expression with respect to the caret position."),
            sublime.ListInputItem("Outermost S-expression", "outermost", details="The outermost S-expression with respect to the caret position.", annotation="ignores (comment)"),
            sublime.ListInputItem("Active view", "view", details="The entire contents of the currently active view."),
            sublime.ListInputItem("Input", "input", details="Tutkain prompts you for input to evaluate."),
            sublime.ListInputItem("Namespace declarations", "ns", details="Every namespace declaration (<code>ns</code> form) in the active view."),
        ]


class TutkainConnectCommand(WindowCommand):
    def set_layout(self):
        # Set up a two-row layout.
        #
        # TODO: Make configurable? This will clobber pre-existing layouts —
        # maybe add a setting for toggling this bit?

        # Only change the layout if the current layout has one row and one column.
        if self.window.get_layout() == {
            "cells": [[0, 0, 1, 1]],
            "cols": [0.0, 1.0],
            "rows": [0.0, 1.0],
        }:
            if settings().get("layout") == "vertical":
                layout = {
                    "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
                    "cols": [0.0, 0.5, 1.0],
                    "rows": [0.0, 1.0],
                }
            else:
                layout = {
                    "cells": [[0, 0, 1, 1], [0, 1, 1, 2]],
                    "cols": [0.0, 1.0],
                    "rows": [0.0, 0.75, 1.0],
                }

            self.window.set_layout(layout)

    def run_commands(self, active_view, then):
        if then:
            for command in then:
                scope = command.get("scope", "window")

                if "command" in command and "args" in command:
                    if scope == "view":
                        active_view.run_command(command["command"], command["args"])
                    else:
                        self.window.run_com
                        mand(command["command"], command["args"])

    def run(self, host, port, view_id=None, then=[]):
        try:
            active_view = self.window.active_view()
            tap.create_panel(self.window)
            client = Client(source_root(), host, int(port)).connect(then=lambda _: self.run_commands(active_view, then))
            self.set_layout()
            view = views.configure(self.window, client, view_id)

            print_loop = Thread(daemon=True, target=printer.print_loop, args=(view, client))
            print_loop.name = "tutkain.print_loop"
            print_loop.start()

            # Activate the output view and the view that was active prior to
            # creating the output view.
            self.window.focus_view(view)
            self.window.focus_view(active_view)
        except TimeoutError:
            sublime.error_message(cleandoc("""
                Timed out trying to connect to socket REPL server.

                Are you trying to connect to an nREPL server? Tutkain no longer supports nREPL.

                See https://tutkain.flowthing.me/#starting-a-socket-repl for more information.
                """))
        except ConnectionRefusedError:
            self.window.status_message(f"ERR: connection to {host}:{port} refused.")

    def input(self, args):
        if "host" in args and "port" in args:
            return None
        elif "host" in args:
            return PortInputHandler(self.window)
        else:
            return HostInputHandler(self.window)


class TutkainDisconnectCommand(WindowCommand):
    def run(self):
        inline.clear(self.window.active_view())
        test.progress.stop()
        view = state.repl_view(self.window)
        view and view.close()


class TutkainNewScratchViewCommand(WindowCommand):
    def run(self):
        view = self.window.new_file()
        view.set_name("*scratch*")
        view.set_scratch(True)
        view.assign_syntax("Clojure (Tutkain).sublime-syntax")
        self.window.focus_view(view)


def completion_kinds():
    return {
        "function": sublime.KIND_FUNCTION,
        "var": sublime.KIND_VARIABLE,
        "macro": (sublime.KIND_ID_FUNCTION, "m", "macro"),
        "multimethod": (sublime.KIND_ID_FUNCTION, "u", "multimethod"),
        "namespace": sublime.KIND_NAMESPACE,
        "class": sublime.KIND_TYPE,
        "special-form": (sublime.KIND_ID_FUNCTION, "s", "special form"),
        "method": sublime.KIND_FUNCTION,
        "static-method": sublime.KIND_FUNCTION,
        "keyword": sublime.KIND_KEYWORD,
    }


class TutkainShowPopupCommand(TextCommand):
    def run(self, edit, item={}):
        d = {}

        for k, v in item.items():
            d[edn.Keyword(k)] = v

        info.show_popup(self.view, -1, {edn.Keyword("info"): d})


class TutkainViewEventListener(ViewEventListener):
    def completion_item(self, item):
        details = ""

        if edn.Keyword("doc") in item:
            d = {}

            for k, v in item.items():
                d[k.name] = v.name if isinstance(v, edn.Keyword) else v

            details = f"""<a href="{sublime.command_url("tutkain_show_popup", args={"item": d})}">More</a>"""

        return sublime.CompletionItem(
            item.get(edn.Keyword("candidate")),
            kind=completion_kinds().get(item.get(edn.Keyword("type")).name, sublime.KIND_AMBIGUOUS),
            annotation=" ".join(item.get(edn.Keyword("arglists"), [])),
            details=details,
        )

    def on_query_completions(self, prefix, locations):
        point = locations[0] - 1

        if settings().get("auto_complete") and self.view.match_selector(
            point,
            "source.clojure & (meta.symbol - meta.function.parameters) | (constant.other.keyword - punctuation.definition.keyword)",
        ) and (client := state.client(self.view.window())):
            if scope := selectors.expand_by_selector(self.view, point, "meta.symbol | constant.other.keyword"):
                prefix = self.view.substr(scope)

            completion_list = sublime.CompletionList()

            client.backchannel.send({
                edn.Keyword("op"): edn.Keyword("completions"),
                edn.Keyword("prefix"): prefix,
                edn.Keyword("ns"): namespace.name(self.view),
                edn.Keyword("dialect"): dialect(self.view, point)
            }, handler=lambda response: (
                completion_list.set_completions(
                    map(self.completion_item, response.get(edn.Keyword("completions"), []))
                )
            ))

            return completion_list


def lookup(view, point, handler):
    is_repl_output_view = view.settings().get("tutkain_repl_output_view")

    if (
        view.match_selector(
            point,
            "source.clojure & (meta.symbol | constant.other.keyword.qualified | constant.other.keyword.auto-qualified)"
        )
        and not is_repl_output_view
    ):
        if (symbol := selectors.expand_by_selector(
            view,
            point,
            "meta.symbol | constant.other.keyword.qualified | constant.other.keyword.auto-qualified"
        )) and (client := state.client(view.window())):
            client.backchannel.send({
                edn.Keyword("op"): edn.Keyword("lookup"),
                edn.Keyword("named"): view.substr(symbol),
                edn.Keyword("ns"): namespace.name(view),
                edn.Keyword("dialect"): dialect(view, point)
            },
                handler=handler
            )


class TutkainShowSymbolInformationCommand(TextCommand):
    def handler(self, response):
        info.show_popup(self.view, self.view.sel()[0].begin(), response)

    def run(self, edit):
        lookup(self.view, self.view.sel()[0].begin(), self.handler)


class TutkainGotoSymbolDefinitionCommand(TextCommand):
    def handler(self, response):
        info.goto(self.view.window(), info.parse_location(response.get(edn.Keyword("info"))))

    def run(self, edit):
        lookup(self.view, self.view.sel()[0].begin(), self.handler)


def reconnect(vs):
    for view in filter(views.is_output_view, vs):
        host = view.settings().get("tutkain_repl_host")
        port = view.settings().get("tutkain_repl_port")

        if host and port:
            view.window().run_command(
                "tutkain_connect", {"host": host, "port": port, "view_id": view.id()}
            )


class TutkainEventListener(EventListener):
    def on_init(self, views):
        reconnect(views)

    def on_load_project_async(self, window):
        reconnect(window.views())

    def on_modified_async(self, view):
        inline.clear(view)

    def on_deactivated_async(self, view):
        inline.clear(view)

    def on_activated(self, view):
        if view.settings().get("tutkain_repl_output_view"):
            state.set_repl_view(view)

    def on_activated_async(self, view):
        if not view.settings().get("is_widget") and view.match_selector(0, "source.clojure") and (client := state.client(view.window())):
            ns = namespace.name(view) or "user"

            if ns != client.namespace:
                client.switch_namespace(ns, dialect(view, 0))
                repl_view = state.repl_view(view.window())
                repl_view.set_name(f"REPL · {ns} · {client.host}:{client.port}")

    def on_hover(self, view, point, hover_zone):
        lookup(view, point, lambda response: info.show_popup(view, point, response))

    def on_close(self, view):
        if view.settings().get("tutkain_repl_output_view"):
            window = sublime.active_window()
            num_groups = window.num_groups()

            if num_groups == 2 and len(window.views_in_group(num_groups - 1)) == 0:
                window.set_layout({
                    'cells': [[0, 0, 1, 1]],
                    'cols': [0.0, 1.0],
                    'rows': [0.0, 1.0]
                })

    def on_pre_close(self, view):
        if view and view.settings().get("tutkain_repl_output_view"):
            window = view.window()
            client = state.view_client(view)

            if client:
                client.halt()
                state.forget_repl_view(view)

                window.destroy_output_panel(tap.panel_name)

            if window:
                active_view = window.active_view()

                if active_view:
                    active_view.run_command("tutkain_clear_test_markers")
                    window.focus_view(active_view)


class TutkainExpandSelectionCommand(TextCommand):
    def run(self, edit):
        view = self.view
        selections = view.sel()

        for region in selections:
            if not region.empty() or selectors.ignore(view, region.begin()):
                view.run_command("expand_selection", {"to": "scope"})
            else:
                form = forms.find_adjacent(view, region.begin())
                form and selections.add(form)


class TutkainInterruptEvaluationCommand(WindowCommand):
    def run(self):
        client = state.client(self.window)

        if client is None:
            self.window.status_message("ERR: Not connected to a REPL.")
        else:
            client.backchannel.send({edn.Keyword("op"): edn.Keyword("interrupt")})


class TutkainInsertNewlineCommand(TextCommand):
    def run(self, edit):
        indent.insert_newline_and_indent(self.view, edit)


class TutkainIndentSexpCommand(TextCommand):
    def run(self, edit, scope="outermost", prune=False):
        for region in self.view.sel():
            if region.empty():
                if scope == "outermost":
                    s = sexp.outermost(self.view, region.begin())
                elif scope == "innermost":
                    s = sexp.innermost(self.view, region.begin())

                if s:
                    indent.indent_region(self.view, edit, s.extent(), prune=prune)
            else:
                indent.indent_region(self.view, edit, region, prune=prune)


class TutkainPareditForwardCommand(TextCommand):
    def run(self, edit):
        paredit.move(self.view, True)


class TutkainPareditBackwardCommand(TextCommand):
    def run(self, edit):
        paredit.move(self.view, False)


class TutkainPareditOpenRoundCommand(TextCommand):
    def run(self, edit):
        paredit.open_bracket(self.view, edit, "(")


class TutkainPareditCloseRoundCommand(TextCommand):
    def run(self, edit):
        paredit.close_bracket(self.view, edit, ")")


class TutkainPareditOpenSquareCommand(TextCommand):
    def run(self, edit):
        paredit.open_bracket(self.view, edit, "[")


class TutkainPareditCloseSquareCommand(TextCommand):
    def run(self, edit):
        paredit.close_bracket(self.view, edit, "]")


class TutkainPareditOpenCurlyCommand(TextCommand):
    def run(self, edit):
        paredit.open_bracket(self.view, edit, "{")


class TutkainPareditCloseCurlyCommand(TextCommand):
    def run(self, edit):
        paredit.close_bracket(self.view, edit, "}")


class TutkainPareditDoubleQuoteCommand(TextCommand):
    def run(self, edit):
        paredit.double_quote(self.view, edit)


class TutkainPareditForwardSlurpCommand(TextCommand):
    def run(self, edit):
        paredit.forward_slurp(self.view, edit)


class TutkainPareditBackwardSlurpCommand(TextCommand):
    def run(self, edit):
        paredit.backward_slurp(self.view, edit)


class TutkainPareditForwardBarfCommand(TextCommand):
    def run(self, edit):
        paredit.forward_barf(self.view, edit)


class TutkainPareditBackwardBarfCommand(TextCommand):
    def run(self, edit):
        paredit.backward_barf(self.view, edit)


class TutkainPareditWrapRoundCommand(TextCommand):
    def run(self, edit):
        paredit.wrap_bracket(self.view, edit, "(")


class TutkainPareditWrapSquareCommand(TextCommand):
    def run(self, edit):
        paredit.wrap_bracket(self.view, edit, "[")


class TutkainPareditWrapCurlyCommand(TextCommand):
    def run(self, edit):
        paredit.wrap_bracket(self.view, edit, "{")


class TutkainPareditForwardDeleteCommand(TextCommand):
    def run(self, edit):
        paredit.forward_delete(self.view, edit)


class TutkainPareditBackwardDeleteCommand(TextCommand):
    def run(self, edit):
        paredit.backward_delete(self.view, edit)


class TutkainPareditRaiseSexpCommand(TextCommand):
    def run(self, edit):
        paredit.raise_sexp(self.view, edit)


class TutkainPareditSpliceSexpCommand(TextCommand):
    def run(self, edit):
        paredit.splice_sexp(self.view, edit)


class TutkainPareditCommentDwimCommand(TextCommand):
    def run(self, edit):
        paredit.comment_dwim(self.view, edit)


class TutkainPareditSemicolonCommand(TextCommand):
    def run(self, edit):
        paredit.semicolon(self.view, edit)


class TutkainPareditSpliceSexpKillingForwardCommand(TextCommand):
    def run(self, edit):
        paredit.splice_sexp_killing_forward(self.view, edit)


class TutkainPareditSpliceSexpKillingBackwardCommand(TextCommand):
    def run(self, edit):
        paredit.splice_sexp_killing_backward(self.view, edit)


class TutkainPareditForwardKillFormCommand(TextCommand):
    def run(self, edit):
        paredit.kill_form(self.view, edit, True)


class TutkainPareditBackwardKillFormCommand(TextCommand):
    def run(self, edit):
        paredit.kill_form(self.view, edit, False)


class TutkainPareditBackwardMoveFormCommand(TextCommand):
    def run(self, edit):
        paredit.backward_move_form(self.view, edit)


class TutkainPareditForwardMoveFormCommand(TextCommand):
    def run(self, edit):
        paredit.forward_move_form(self.view, edit)


class TutkainPareditThreadFirstCommand(TextCommand):
    def run(self, edit):
        paredit.thread_first(self.view, edit)


class TutkainPareditThreadLastCommand(TextCommand):
    def run(self, edit):
        paredit.thread_last(self.view, edit)


class TutkainPareditForwardUpCommand(TextCommand):
    def run(self, edit):
        paredit.forward_up(self.view, edit)


class TutkainPareditForwardDownCommand(TextCommand):
    def run(self, edit):
        paredit.forward_down(self.view, edit)


class TutkainPareditBackwardUpCommand(TextCommand):
    def run(self, edit):
        paredit.backward_up(self.view, edit)


class TutkainPareditBackwardDownCommand(TextCommand):
    def run(self, edit):
        paredit.backward_down(self.view, edit)


class TutkainCycleCollectionTypeCommand(TextCommand):
    def run(self, edit):
        sexp.cycle_collection_type(self.view, edit)


class TutkainDiscardUndiscardSexpCommand(TextCommand):
    def run(self, edit, scope="innermost"):
        paredit.discard_undiscard(self.view, edit, scope)


class TutkainReplHistoryListener(EventListener):
    def on_deactivated(self, view):
        if view.settings().get("tutkain_repl_input_panel"):
            history.index = None


class TutkainNavigateReplHistoryCommand(TextCommand):
    def run(self, edit, forward=False):
        history.navigate(self.view, edit, forward=forward)


class TutkainClearTestMarkersCommand(TextCommand):
    def run(self, edit):
        self.view.erase_regions(test.region_key(self.view, "passes"))
        self.view.erase_regions(test.region_key(self.view, "failures"))
        self.view.erase_regions(test.region_key(self.view, "errors"))


class TutkainOpenDiffWindowCommand(TextCommand):
    def run(self, edit, reference="", actual=""):
        self.view.window().run_command("new_window")

        window = sublime.active_window()
        window.set_tabs_visible(False)
        window.set_minimap_visible(False)
        window.set_status_bar_visible(False)
        window.set_sidebar_visible(False)
        window.set_menu_visible(False)

        view = window.new_file()
        view.set_name("Tutkain: Diff")
        view.assign_syntax("Clojure (Tutkain).sublime-syntax")
        view.set_scratch(True)
        view.set_reference_document(reference)
        view.run_command("append", {"characters": actual})
        view.set_read_only(True)

        # Hackity hack to try to ensure that the inline diff is open when the diff window opens.
        #
        # I have no idea why this works, or whether it actually even works.
        view.run_command("next_modification")
        view.show(0)

        view.run_command("toggle_inline_diff")


class TutkainShowUnsuccessfulTestsCommand(TextCommand):
    def get_preview(self, region):
        line = self.view.rowcol(region.begin())[0] + 1
        preview = self.view.substr(self.view.line(region)).lstrip()
        return f"{line}: {preview}"

    def run(self, args):
        view = self.view
        failures = test.regions(view, "failures")
        errors = test.regions(view, "errors")
        regions = failures + errors

        if regions:
            regions.sort()

            def goto(i):
                view.set_viewport_position(view.text_to_layout(regions[i].begin()))

            view.window().show_quick_panel(
                [self.get_preview(region) for region in regions],
                goto,
                flags=sublime.MONOSPACE_FONT,
                on_highlight=goto,
            )


class TutkainInitializeClojurescriptSupportCommand(WindowCommand):
    def set_build_id(self, client, build_id):
        client.backchannel.options["shadow-build-id"] = build_id
        client.recvq.put({edn.Keyword("val"): f":{build_id.name}\n"})

    def choose_build_id(self, client, response):
        items = list(map(lambda id: id.name, response.get(edn.Keyword("build-ids", "shadow"), [])))

        if items:
            self.window.show_quick_panel(
                items,
                lambda index: self.set_build_id(client, edn.Keyword(items[index])),
                placeholder="Choose shadow-cljs build ID"
            )

    def enable_handler(self, client, response, build_id=None):
        if build_id is None:
            handler = lambda response: self.choose_build_id(client, response)
        else:
            handler = lambda _: self.set_build_id(client, build_id)

        val = response.get(edn.Keyword("val"))

        if val and edn.read(val):
            client.backchannel.send({edn.Keyword("op"): edn.Keyword("initialize-cljs")}, handler=handler)

    def run(self, build_id=None):
        client = state.client(self.window)

        if client is None:
            self.window.status_message("ERR: Not connected to a REPL.")
        else:
            client.initialize_cljs(lambda response: self.enable_handler(client, response, build_id))
