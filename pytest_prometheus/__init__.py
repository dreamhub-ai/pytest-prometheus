import logging
import re
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

def pytest_addoption(parser):
    group = parser.getgroup('terminal reporting')
    group.addoption(
        '--prometheus-pushgateway-url',
        help='Push Gateway URL to send metrics to'
    )
    group.addoption(
        '--prometheus-metric-prefix',
        help='Prefix for all prometheus metrics'
    )
    group.addoption(
        '--prometheus-extra-label',
        action='append',
        help='Extra labels to attach to reported metrics'
    )
    group.addoption(
        '--prometheus-job-name',
        help='Value for the "job" key in exported metrics'
    )

def pytest_configure(config):
    if config.getoption('prometheus_pushgateway_url') and config.getoption('prometheus_metric_prefix'):
        config._prometheus = PrometheusReport(config)
        config.pluginmanager.register(config._prometheus)

def pytest_unconfigure(config):
    prometheus = getattr(config, '_prometheus', None)

    if prometheus:
        del config._prometheus
        config.pluginmanager.unregister(prometheus)


class PrometheusReport:
    def __init__(self, config):
        self.config = config
        self.prefix = config.getoption('prometheus_metric_prefix')
        self.pushgateway_url = config.getoption('prometheus_pushgateway_url')
        self.job_name = config.getoption('prometheus_job_name')

        # Initialize extra_labels
        extra_labels_raw = config.getoption('prometheus_extra_label') or []
        self.extra_labels = {}
        for label in extra_labels_raw:
            try:
                key, value = label.split('=', 1)
                self.extra_labels[key] = value
            except ValueError:
                logging.warning(f"Skipping invalid label format: {label}")

        # Initialize registry
        self.registry = CollectorRegistry()

        # Initialize counters
        self.test_results = {}  # Store test results by name and outcome

    def _make_metric_name(self, name):
        unsanitized_name = f'{self.prefix}{name}'
        pattern = r'[^a-zA-Z0-9_]'
        replacement = '_'
        return re.sub(pattern, replacement, unsanitized_name)

    def _make_labels(self, testname):
        labels = self.extra_labels.copy()
        labels["testname"] = testname
        return labels

    def _get_label_names(self):
        return list(self._make_labels("").keys())

    def pytest_runtest_logreport(self, report):
        try:
            if not hasattr(report, 'location'):
                return

            funcname = report.location[2]
            name = self._make_metric_name(funcname)

            if not name in self.test_results:
                self.test_results[name] = {'status': None, 'phase': None}

            # Skip if we've already recorded a final status for this test
            current_status = self.test_results[name]['status']
            if current_status in ('failed', 'passed'):
                return

            # Record the test outcome
            if report.when == 'call':
                if report.outcome == 'passed':
                    self.test_results[name]['status'] = 'passed'
                elif report.outcome == 'failed':
                    self.test_results[name]['status'] = 'failed'
            elif report.when == 'setup':
                if report.outcome == 'skipped':
                    self.test_results[name]['status'] = 'skipped'
                elif report.outcome == 'failed':
                    self.test_results[name]['status'] = 'failed'

        except Exception as e:
            logging.error(f"Error processing test report: {e}")

    def pytest_sessionfinish(self, session):
        try:
            # Prepare metric data
            passed = []
            failed = []
            skipped = []
            total = list(self.test_results.keys())

            # Categorize tests based on their final status
            for name, result in self.test_results.items():
                status = result['status']
                if status == 'passed':
                    passed.append(name)
                elif status == 'failed':
                    failed.append(name)
                elif status == 'skipped':
                    skipped.append(name)

            # Create and populate metrics
            label_names = self._get_label_names()

            metrics = [
                ('total', total, "Total number of tests executed"),
                ('passed', passed, "Number of passed tests"),
                ('failed', failed, "Number of failed tests"),
                ('skipped', skipped, "Number of skipped tests")
            ]

            for metric_name, test_list, description in metrics:
                gauge = Gauge(
                    self._make_metric_name(metric_name),
                    description,
                    labelnames=label_names,
                    registry=self.registry
                )

                for test in test_list:
                    try:
                        labels = self._make_labels(test)
                        gauge.labels(**labels).inc()
                    except Exception as e:
                        logging.error(f"Error incrementing metric for test {test}: {e}")

            # Push metrics to gateway
            push_to_gateway(self.pushgateway_url, registry=self.registry, job=self.job_name)

        except Exception as e:
            logging.error(f"Error in session finish: {e}")

    def pytest_terminal_summary(self, terminalreporter):
        # Write to the pytest terminal
        terminalreporter.write_sep('-',
                'Sending test results to Prometheus pushgateway at {url}'.format(
                    url=self.pushgateway_url))

