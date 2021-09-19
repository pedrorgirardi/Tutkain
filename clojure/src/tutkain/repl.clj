(ns tutkain.repl
  (:require
   [clojure.main :as main]
   [tutkain.backchannel :as backchannel]
   [tutkain.format :as format])
  (:import
   (java.io File)
   (java.net URL)))

(def ^:dynamic ^:experimental *print*
  "A function you can use as the :print arg of clojure.main/repl."
  prn)

(def ^:dynamic ^:experimental *caught*
  "A function you can use as the :caught arg of clojure.main/repl."
  main/repl-caught)

(defn repl
  "Tutkain's main read-eval-print loop.

  Like clojure.core.server/io-prepl, with these differences:

  - Starts a backchannel socket server that Tutkain uses for editor tooling
    (auto-completion, metadata lookup, etc.)
  - Pretty-prints evaluation results and exception maps
  - Binds *print* and *caught* for use with nested REPLs started via
    clojure.main/repl
  - Binds *file* and *source-path* to vals sent via backchannel when evaluating
    to ensure useful exception stack traces"
  ([]
   (repl {}))
  ([opts]
   (let [EOF (Object.)
         lock (Object.)
         out *out*
         in *in*
         out-fn (fn [message]
                  (binding [*out* out
                            *flush-on-newline* true
                            *print-readably* true]
                    (locking lock
                      (prn message))))
         tapfn #(out-fn {:tag :tap :val (format/pp-str %1)})
         repl-thread (Thread/currentThread)]
     (main/with-bindings
       (in-ns 'user)
       (apply require main/repl-requires)
       (binding [*out* (PrintWriter-on #(out-fn {:tag :out :val %1}) nil)
                 *err* (PrintWriter-on #(out-fn {:tag :err :val %1}) nil)
                 *print* #(out-fn {:tag :ret :val (format/pp-str %)})
                 *caught* #(out-fn {:tag :err :val (format/Throwable->str %)})]
         (let [backchannel (backchannel/open
                             (assoc opts
                               :xform-in #(assoc % :in in :repl-thread repl-thread)
                               :xform-out #(dissoc % :in)))]
           (try
             (let [address (.getLocalAddress backchannel)]
               (out-fn {:tag :ret
                        :val (pr-str {:host (.getHostName address)
                                      :port (.getPort address)})}))
             (add-tap tapfn)
             (loop []
               (when
                 (try
                   (let [[form s] (read+string {:eof EOF :read-cond :allow} in)
                         file (backchannel/eval-context :file)]
                     (binding [*file* (or file "NO_SOURCE_PATH")
                               *source-path* (or (some-> file File. .getName) "NO_SOURCE_FILE")]
                       (try
                         (when-not (identical? form EOF)
                           (let [start (System/nanoTime)
                                 ret (eval form)
                                 ms (quot (- (System/nanoTime) start) 1000000)]
                             (when-not (= :repl/quit ret)
                               (set! *3 *2)
                               (set! *2 *1)
                               (set! *1 ret)
                               (out-fn {:tag :ret
                                        :val (format/pp-str ret)
                                        :ns (str (.name *ns*))
                                        :ms ms
                                        :form s})
                               true)))
                         (catch Throwable ex
                           (set! *e ex)
                           (out-fn {:tag :err
                                    :val (format/Throwable->str ex)
                                    :ns (str (.name *ns*))
                                    :form s})
                           true))))
                   (catch Throwable ex
                     (set! *e ex)
                     (out-fn {:tag :ret
                              :val (format/pp-str (assoc (Throwable->map ex) :phase :read-source))
                              :ns (str (.name *ns*))
                              :exception true})
                     true))
                 (recur)))
             (finally
               (.close backchannel)
               (remove-tap tapfn)))))))))

(defn class->source
  "Given the URL of an artifact in the classpath providing Java classes, return
  the URL of the JAR that contains the source code of those classes.

  Assumes that the source JAR has been generated by the Maven Source plugin.

  (TODO: Read actual path from artifact POM file?)

  See https://maven.apache.org/plugins/maven-source-plugin/examples/configureplugin.html."
  [^URL url]
  (some-> url
    .toString
    (.replace ".jar!" "-sources.jar!")
    (.replace ".class" ".java")
    URL.))

(defn resolve-stacktrace
  "Given a java.lang.Throwable, return a seq of maps representing its resolved
  stack trace.

  A \"resolved\" stack trace is one where each element of the stack trace has
  an absolute path to the source file of the stack trace element.

  For Java sources, presumes that the Java source files are next to the main
  artifact in the local Maven repository."
  [^Throwable ex]
  (let [cl (.getContextClassLoader (Thread/currentThread))]
    (map (fn [el]
           (let [class-name (.getClassName el)
                 file-name (.getFileName el)
                 url (if (.endsWith file-name ".java")
                       (class->source (.getResource cl (str (.replace class-name "." "/") ".class")))
                       (or
                         ;; TODO: This appears to work, but is it right?
                         (.getResource (Class/forName class-name) file-name)
                         (.getResource cl file-name)))]
             {:file (str url)
              :file-name file-name
              :column 1
              :name (str class-name "/" (.getMethodName el))
              :line (.getLineNumber el)}))
      (.getStackTrace ex))))
