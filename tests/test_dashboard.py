import io
import unittest
from datetime import datetime, timedelta, timezone

from victron import LiveDashboard


class LiveDashboardTests(unittest.TestCase):
    def test_forwards_output_and_renders_latest_smartshunt_values(self):
        forwarded = []
        stream = io.StringIO()
        dashboard = LiveDashboard('Test SmartShunt', lambda *args: forwarded.append(args), stream=stream, start=False)

        dashboard('Test SmartShunt', 'Voltage', 12.34, vunit='V')
        dashboard('Test SmartShunt', 'Current', -1.25, vunit='A')
        dashboard('Test SmartShunt', 'Power', -15, vunit='W')
        dashboard('Test SmartShunt', 'State Of Charge', 99.5, vunit='%')
        dashboard('Test SmartShunt', 'Last Update', datetime.now().astimezone().isoformat(), vunit='timestamp')
        dashboard.render()

        screen = stream.getvalue()
        self.assertEqual(len(forwarded), 5)
        self.assertIn('Voltage            12.34 V', screen)
        self.assertIn('Current            -1.25 A', screen)
        self.assertIn('Power              -15 W', screen)
        self.assertIn('State Of Charge    99.5 %', screen)
        self.assertIn('Last device update:  0s ago', screen)

    def test_age_formatting_handles_missing_and_past_timestamps(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(LiveDashboard._format_age(None, now), 'waiting for data')
        self.assertEqual(LiveDashboard._format_age(now - timedelta(seconds=7), now), '7s ago')


if __name__ == '__main__':
    unittest.main()