import sublime
import sublime_plugin
import logging
from threading import Thread

from . import brackets
from .repl_client import ReplClient


def debug_mode():
    logging.basicConfig(
        level=logging.DEBUG,
        format=' %(asctime)s - %(levelname)s - %(message)s'
        )


repl_client = None


def print_characters(panel, characters):
    if characters is not None:
        panel.run_command('append', {
            'characters': characters.strip() + '\n',
            'scroll_to_end': True
        })


def print_comment(panel, out):
    print_characters(
        panel,
        '\n'.join(
            map(lambda line: ';; {}'.format(line),
                out.splitlines())
            )
        )


def append_to_output_panel(window, message):
    if message:
        panel = window.find_output_panel('panel')

        panel.set_read_only(False)

        if 'value' in message:
            print_characters(panel, message.get('value'))
        if 'nrepl.middleware.caught/throwable' in message:
            throwable = message.get('nrepl.middleware.caught/throwable')
            print_characters(panel, throwable)
        if 'out' in message:
            print_comment(panel, message['out'])
        if 'err' in message:
            print_comment(panel, message['err'])
        if 'in' in message:
            print_comment(panel, '=> {}'.format(message['in']))

        panel.set_read_only(True)

        panel.run_command('move_to', {'to': 'eof'})


class TutkainClearOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        panel = self.window.find_output_panel('panel')
        panel.set_read_only(False)
        panel.run_command('select_all')
        panel.run_command('right_delete')
        panel.set_read_only(True)


class TutkainEvaluateFormCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global repl_client

        if repl_client is None:
            self.view.window().status_message('ERR: Not connected to a REPL.')
        else:
            for region in self.view.sel():
                eval_region = region

                if eval_region.empty():
                    eval_region = brackets.current_form_region(
                        self.view,
                        region.begin()
                    )

                if eval_region is not None:
                    self.view.window().run_command(
                        'show_panel',
                        {'panel': 'output.panel'}
                    )

                    chars = self.view.substr(eval_region)
                    append_to_output_panel(self.view.window(), {'in': chars})

                    logging.debug({
                        'event': 'send',
                        'scope': 'form',
                        'data': chars
                    })

                    repl_client.input.put(
                        repl_client.user_session.eval_op({
                            'code': chars
                        })
                    )


class TutkainEvaluateViewCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global repl_client

        if repl_client is None:
            self.view.window().status_message('ERR: Not connected to a REPL.')
        else:
            region = sublime.Region(0, self.view.size())

            repl_client.input.put(
                repl_client.user_session.eval_op({
                    'code': self.view.substr(region)
                })
            )


class HostInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, window):
        self.window = window

    def placeholder(self):
        return 'Host'

    def validate(self, text):
        return len(text) > 0

    def initial_text(self):
        return 'localhost'

    def next_input(self, host):
        return PortInputHandler(self.window)


class PortInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, window):
        self.window = window

    def placeholder(self):
        return 'Port'

    def validate(self, text):
        return text.isdigit()


class TutkainToggleOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        active_panel = self.window.active_panel()
        panel = 'output.panel'

        if active_panel == panel:
            self.window.run_command('hide_panel', {'panel': panel})
        else:
            self.window.run_command('show_panel', {'panel': panel})


class TutkainEvaluateInputCommand(sublime_plugin.WindowCommand):
    def eval(self, input):
        global repl_client

        if repl_client is None:
            self.window.status_message('ERR: Not connected to a REPL.')
        else:
            self.window.run_command('show_panel', {'panel': 'output.panel'})
            append_to_output_panel(self.window, {'in': input})

            repl_client.input.put(
                repl_client.user_session.eval_op({
                    'code': input
                })
            )

    def noop(*args):
        pass

    def run(self):
        self.window.show_input_panel(
            'Input: ',
            '',
            self.eval,
            self.noop,
            self.noop
        )


class TutkainConnectCommand(sublime_plugin.WindowCommand):
    def configure_output_panel(self):
        panel = self.window.find_output_panel('panel')
        if panel is None:
            panel = self.window.create_output_panel('panel')

        panel.set_name('panel')
        panel.settings().set('line_numbers', False)
        panel.settings().set('gutter', False)
        panel.settings().set('is_widget', True)
        panel.settings().set('scroll_past_end', False)
        panel.set_read_only(True)
        panel.assign_syntax('Packages/Clojure/Clojure.sublime-syntax')

    def print_loop(self, repl_client):
        while True:
            item = repl_client.output.get()

            if item is None:
                break

            logging.debug({'event': 'printer/recv', 'data': item})

            versions = item.get('versions')

            if versions:
                clojure_version = versions.get('clojure').get('version-string')
                nrepl_version = versions.get('nrepl').get('version-string')

                append_to_output_panel(
                    self.window,
                    {'out': 'Clojure {}'.format(clojure_version)}
                )

                append_to_output_panel(
                    self.window,
                    {'out': 'nREPL {}'.format(nrepl_version)}
                )

            append_to_output_panel(self.window, item)

        logging.debug({'event': 'thread/exit', 'thread': 'print_loop'})

    def run(self, host, port):
        global repl_client

        try:
            repl_client = ReplClient(host, int(port))
            repl_client.go()

            # Start a worker that reads values from a ReplClient output queue
            # and prints them into an output panel.
            Thread(
                daemon=True,
                target=self.print_loop,
                args=(repl_client,)
            ).start()

            # Create an output panel for printing evaluation results and show
            # it.
            self.configure_output_panel()
            self.window.run_command('show_panel', {'panel': 'output.panel'})

            message = 'Connected to {}:{}.'.format(host, port)
            append_to_output_panel(self.window, {'out': message})
        except ConnectionRefusedError:
            self.window.status_message(
                'ERR: connection to {}:{} refused.'.format(host, port)
            )

    def input(self, args):
        return HostInputHandler(self.window)


class TutkainDisconnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        global repl_client

        if repl_client is not None:
            repl_client.halt()
            repl_client = None

            append_to_output_panel(self.window, {'out': 'Disconnected.'})
