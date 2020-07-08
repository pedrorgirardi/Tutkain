(ns tutkain.nrepl.middleware.test
  (:require
   [clojure.pprint :as pprint]
   [clojure.stacktrace :as stacktrace]
   [clojure.string :as str]
   [clojure.test :as test]
   [nrepl.middleware :as middleware]
   [nrepl.misc :refer [response-for]]
   [nrepl.transport :as transport]))


(defn- pprint-expected
  [{:keys [actual expected] :as event}]
  (if (and (= (first expected) '=) (sequential? actual))
    (assoc event :expected (->> actual last second pprint/pprint with-out-str))
    (update event :expected str)))


(defn- pprint-actual
  [event]
  (update event :actual (comp #(with-out-str (pprint/pprint %)) last last)))


(defn- event-var-meta
  [event]
  (-> event :var meta (select-keys [:line :column :file :name :ns]) (update :ns str)))


(defn- class-name-prefix
  [ns]
  (str/replace (format "%s$" ns) "-" "_"))


(defn- line-number
  [ns]
  (->> (.getStackTrace (new java.lang.Throwable))
    (drop-while #(not (str/starts-with? (.getClassName ^StackTraceElement %)
                        (class-name-prefix ns))))
    (first)
    (.getLineNumber)))


(defn- add-result
  [results result]
  (swap! results update :results (fnil conj []) result))


(defn wrap-test
  [handler]
  (fn [{:keys [op transport ns file] :as message}]
    (case op
      "tutkain/test"
      (let [results (atom {:results []})
            var-meta (atom nil)]
        (binding [test/report (fn [event*]
                                (let [{:keys [type] :as event} (cond-> event* (seq file) (assoc :file file))]
                                  ;; TODO: Could use a multimethod here instead.
                                  (case type
                                    :begin-test-var (reset! var-meta (event-var-meta event))
                                    :end-test-var (reset! var-meta nil)
                                    :fail (do
                                            (test/inc-report-counter :fail)
                                            (add-result results (-> event pprint-expected pprint-actual (assoc :var-meta @var-meta))))
                                    :pass (do
                                            (test/inc-report-counter :pass)
                                            (add-result results {:type :pass :line (line-number ns) :var-meta @var-meta}))
                                    :error (do
                                             (test/inc-report-counter :error)
                                             (add-result results
                                               (-> event pprint-expected (update :actual #(with-out-str (stacktrace/print-stack-trace %))) (assoc :var-meta @var-meta))))
                                    :summary (swap! results assoc :value (-> event (dissoc :type :file) str))
                                    nil)))]
          (test/run-tests (symbol ns)))
        (transport/send transport (response-for message @results))
        (transport/send transport (response-for message {:status :done})))
      (handler message))))


(middleware/set-descriptor! #'wrap-test
  {:requires #{"clone"}
   :expects #{}
   :handles {"tutkain/test" {:doc "Tutkain clojure.test integration middleware."
                             :requires {"ns" "The name of the namespace with the tests."}
                             :optional {"file" "The path to the Clojure source file with the tests."}
                             :returns {"status" "'done' when all tests have been run and results have been delivered to the client."
                                       "results" "A map with test results."}}}})
