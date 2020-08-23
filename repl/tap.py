def panel_name(window, client):
    return client and 'tutkain.{}'.format(client.id())


def find_panel(window, client):
    name = panel_name(window, client)
    return name and window.find_output_panel(name)


def show_panel(window, client):
    name = panel_name(window, client)
    name and window.run_command('show_panel', {'panel': 'output.{}'.format(name)})
