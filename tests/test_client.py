from unittest import TestCase

from Tutkain.api import edn
from Tutkain.package import source_root, start_logging, stop_logging
from Tutkain.src.repl.client import JVMClient

from .mock import Server


class TestJVMClient(TestCase):
    @classmethod
    def setUpClass(self):
        start_logging(False)

    @classmethod
    def tearDownClass(self):
        stop_logging()

    def test_smoke(self):
        with Server(greeting="user=> ") as server:
            with JVMClient(source_root(), server.host, server.port, wait=False) as client:
                # Client starts sub-REPL
                server.recv()

                with Server() as backchannel:
                    server.send(
                        edn.kwmap({
                            "tag": edn.Keyword("ret"),
                            "val": f"""{{:host "localhost", :port {backchannel.port}}}""",
                        })
                    )

                    for filename in ["lookup.clj", "completions.clj", "load_blob.clj", "test.clj", "cljs.clj", "shadow.clj"]:
                        response = edn.read(backchannel.recv())
                        self.assertEquals(edn.Keyword("load-base64"), response.get(edn.Keyword("op")))
                        self.assertEquals(filename, response.get(edn.Keyword("filename")))

                    self.assertEquals(
                        JVMClient.handshake_payloads["print_version"],
                        server.recv().rstrip()
                    )

                    server.send({
                        edn.Keyword("tag"): edn.Keyword("out"),
                        edn.Keyword("val"): "Clojure 1.11.0-alpha1"
                    })

                    server.send({
                        edn.Keyword("tag"): edn.Keyword("ret"),
                        edn.Keyword("val"): "nil",
                        edn.Keyword("ns"): "user",
                        edn.Keyword("ms"): 0,
                        edn.Keyword("form"): JVMClient.handshake_payloads["print_version"]
                    })

                    self.assertEquals({
                        "printable": "Clojure 1.11.0-alpha1\n",
                        "response": {
                            edn.Keyword("tag"): edn.Keyword("out"),
                            edn.Keyword("val"): "Clojure 1.11.0-alpha1"
                        }
                    }, client.printq.get(timeout=1))

                    client.eval("(inc 1)")

                    response = edn.read(backchannel.recv())
                    id = response.get(edn.Keyword("id"))

                    self.assertEquals(
                        edn.kwmap({
                            "op": edn.Keyword("set-eval-context"),
                            "id": id,
                            "file": "NO_SOURCE_FILE",
                            "ns": edn.Symbol("user"),
                            "line": 1,
                            "column": 1
                        }),
                        response
                    )

                    backchannel.send(edn.kwmap({
                        "id": id,
                        "file": None,
                        "ns": edn.Symbol("user"),
                        "dialect": edn.Keyword("clj")
                    }))

                    self.assertEquals("(inc 1)\n", server.recv())

                    response = edn.kwmap({
                        "tag": edn.Keyword("ret"),
                        "val": "2",
                        "ns": "user",
                        "ms": 1,
                        "form": "(inc 1)"
                    })

                    server.send(response)

                    self.assertEquals(
                        {"printable": "2\n", "response": response},
                        client.printq.get(timeout=1)
                    )

        self.assertEquals(":repl/quit\n", server.recv())
