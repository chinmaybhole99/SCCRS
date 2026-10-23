"""
SCCRS Selenium end-to-end smoke tests.
Prerequisites:
  1. Start the Flask app in another terminal: python app.py
  2. Install Selenium: python -m pip install selenium
  3. Run this script: python test_sccrs_selenium.py

Uses the demo admin account and creates unique test users.
Run only against a development/test database, not production data.
"""
import time
import unittest
import sys
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


BASE_URL = "http://127.0.0.1:5000"
ADMIN_EMAIL = "admin@campus.edu"
ADMIN_PASSWORD = "Admin@123"
WAIT_SECONDS = 10


class SCCRSSeleniumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.ChromeOptions()
        # Uncomment the next line to run without opening a visible browser:
        # options.add_argument("--headless=new")
        options.add_argument("--start-maximized")
        cls.driver = webdriver.Chrome(options=options)  # Selenium Manager handles driver
        cls.wait = WebDriverWait(cls.driver, WAIT_SECONDS)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        cls.student_name = "Selenium Test Student"
        cls.student_id = "SEL" + stamp
        cls.student_email = f"selenium.student.{stamp}@example.com"
        cls.student_password = "Student@123"
        cls.officer_name = "Selenium Facilities Officer"
        cls.officer_email = f"selenium.officer.{stamp}@example.com"
        cls.officer_password = "Officer@123"
        cls.ticket_id = None

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "driver"):
            cls.driver.quit()

    def setUp(self):
        # 500 ms pause before each test case starts.
        time.sleep(0.5)

    def tearDown(self):
        # 500 ms pause after each test case finishes, before its status is printed.
        time.sleep(0.5)

    def open(self, path):
        self.driver.get(BASE_URL + path)

    def wait_for(self, by, selector):
        return self.wait.until(EC.presence_of_element_located((by, selector)))

    def click_text_link(self, text):
        self.wait.until(
            EC.element_to_be_clickable((By.LINK_TEXT, text))
        ).click()

    def login(self, email, password):
        self.open("/login")
        self.wait_for(By.NAME, "email").send_keys(email)
        self.driver.find_element(By.NAME, "password").send_keys(password)
        self.driver.find_element(By.CSS_SELECTOR, "form button").click()
        self.wait.until(lambda d: "/login" not in d.current_url)

    def logout(self):
        self.open("/logout")
        self.wait.until(EC.url_contains("/login"))

    def test_01_invalid_login_is_rejected(self):
        self.open("/login")
        self.wait_for(By.NAME, "email").send_keys("not.registered@example.com")
        self.driver.find_element(By.NAME, "password").send_keys("WrongPass123")
        self.driver.find_element(By.CSS_SELECTOR, "form button").click()
        self.wait.until(
            EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Invalid email or password")
        )
        self.assertIn("/login", self.driver.current_url)

    def test_02_admin_can_log_in(self):
        self.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertIn("dashboard", self.driver.current_url)

    def test_03_admin_can_create_department_officer(self):
        # Admin login is repeated so this test can also run by itself after test 02.
        self.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self.open("/staff/new")
        self.wait_for(By.NAME, "name").send_keys(self.officer_name)
        self.driver.find_element(By.NAME, "email").send_keys(self.officer_email)
        self.driver.find_element(By.NAME, "password").send_keys(self.officer_password)
        Select(self.driver.find_element(By.NAME, "role")).select_by_value("officer")
        Select(self.driver.find_element(By.NAME, "department_id")).select_by_visible_text("Facilities")
        self.driver.find_element(By.CSS_SELECTOR, "form button").click()
        self.wait.until(
            EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Staff account created")
        )

    def test_04_student_can_register(self):
        self.open("/register")
        self.wait_for(By.NAME, "name").send_keys(self.student_name)
        self.driver.find_element(By.NAME, "student_id").send_keys(self.student_id)
        self.driver.find_element(By.NAME, "email").send_keys(self.student_email)
        self.driver.find_element(By.NAME, "password").send_keys(self.student_password)
        self.driver.find_element(By.CSS_SELECTOR, "form button").click()
        self.wait.until(
            EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Registration successful")
        )

    def test_05_student_can_log_in(self):
        self.login(self.student_email, self.student_password)
        self.wait.until(
            EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "My complaints")
        )

    def test_06_student_can_submit_complaint_and_get_ticket(self):
        # Create a separate unique student so this test is repeatable.
        test_email = "x" + self.student_email
        test_student_id = self.student_id + "X"
        self.open("/register")
        self.wait_for(By.NAME, "name").send_keys(self.student_name + " Complaint")
        self.driver.find_element(By.NAME, "student_id").send_keys(test_student_id)
        self.driver.find_element(By.NAME, "email").send_keys(test_email)
        self.driver.find_element(By.NAME, "password").send_keys(self.student_password)
        self.driver.find_element(By.CSS_SELECTOR, "form button").click()
        self.wait.until(lambda d: "/login" in d.current_url or "already exists" in d.find_element(By.TAG_NAME, "body").text.lower())

        self.login(test_email, self.student_password)
        self.open("/complaints/new")

        complaint_form = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//form[.//select[@name='department_id']]")
        ))
        department = Select(complaint_form.find_element(By.NAME, "department_id"))
        department.select_by_visible_text("Facilities")
        priority = Select(complaint_form.find_element(By.NAME, "priority"))
        priority.select_by_visible_text("High")
        description = complaint_form.find_element(By.NAME, "description")
        description.clear()
        description.send_keys("Selenium test: classroom fan is not working and needs repair.")

        # Ensure browser form values are selected before submitting.
        self.assertTrue(department.first_selected_option.text.strip() == "Facilities",
                        "Facilities department was not selected.")
        self.assertTrue(priority.first_selected_option.text.strip() == "High",
                        "High priority was not selected.")
        self.assertGreaterEqual(len(description.get_attribute("value").strip()), 10,
                                "Complaint description is too short.")

        old_url = self.driver.current_url
        complaint_form.find_element(By.CSS_SELECTOR, "button[type='submit'], button").click()

        # Successful submission redirects to the dashboard. If it stays on the
        # form, capture the actual validation message rather than assuming success.
        try:
            self.wait.until(lambda d: "/dashboard" in d.current_url and d.current_url != old_url)
        except TimeoutException:
            body_after = self.driver.find_element(By.TAG_NAME, "body").text
            print("PAGE AFTER SUBMISSION:", body_after, flush=True)
            self.fail("Complaint submission did not redirect to the dashboard. "
                      "Check the visible validation message and Flask terminal output.")

        body = self.driver.find_element(By.TAG_NAME, "body").text
        import re
        match = re.search(r"TCK-\d{8}-[A-F0-9]+", body)
        if not match:
            match = re.search(r"TCK-\d{8}-[A-F0-9]+", self.driver.page_source)
        self.assertIsNotNone(match, "Complaint was submitted but a ticket ID was not found on the dashboard.")
        self.__class__.ticket_id = match.group(0)
        self.__class__.complaint_student_email = test_email
        self.assertIn(self.__class__.ticket_id, self.driver.page_source)

    def test_07_assigned_officer_name_is_visible(self):
        # This checks the dashboard after a complaint is filed by a student.
        # It uses the student created in test 06 (run the script in order).
        ticket_id = self.__class__.ticket_id
        self.assertIsNotNone(ticket_id, "Test 06 must create a ticket before this test.")
        self.open("/dashboard")
        self.wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, "body"), ticket_id))
        self.assertIn(self.officer_name, self.driver.page_source)

    def test_08_officer_can_see_assigned_complaint(self):
        ticket_id = self.__class__.ticket_id
        self.assertIsNotNone(ticket_id, "Test 06 must create a ticket before this test.")

        # Ensure the test officer owns this ticket so the next officer-only test
        # is testing access/status functionality, not a random workload tie-break.
        self.logout()
        self.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self.open("/complaint/" + ticket_id)
        assign_form = self.wait.until(EC.presence_of_element_located((
            By.XPATH, "//form[.//input[@name='action' and @value='assign']]"
        )))
        officer_select = Select(assign_form.find_element(By.NAME, "officer_id"))
        matching_options = [o for o in officer_select.options
                            if self.officer_email.lower() in o.text.lower()]
        self.assertTrue(matching_options,
                        f"Test officer {self.officer_email} is not available for this department.")
        officer_select.select_by_value(matching_options[0].get_attribute("value"))
        assign_form.find_element(By.TAG_NAME, "button").click()
        self.wait.until(EC.text_to_be_present_in_element(
            (By.TAG_NAME, "body"), "Complaint assigned"
        ))

        self.logout()
        self.login(self.officer_email, self.officer_password)
        self.wait.until(EC.text_to_be_present_in_element(
            (By.TAG_NAME, "body"), ticket_id
        ))
        self.assertIn(ticket_id, self.driver.page_source)

    def test_09_officer_can_update_status(self):
        ticket_id = self.__class__.ticket_id
        self.assertIsNotNone(ticket_id, "Test 06 must create a ticket before this test.")
        # Ensure the session is the assigned officer's session.
        if self.driver.current_url.endswith("/login") or "officer workspace" not in self.driver.find_element(By.TAG_NAME, "body").text.lower():
            self.logout()
            self.login(self.officer_email, self.officer_password)
        self.open("/complaint/" + ticket_id)
        status_form = self.wait.until(EC.presence_of_element_located((
            By.XPATH, "//form[.//input[@name='action' and @value='status']]"
        )))
        status_select = Select(status_form.find_element(By.NAME, "status"))
        status_select.select_by_visible_text("In Progress")
        status_form.find_element(By.NAME, "remarks").send_keys(
            "Selenium test: maintenance team has started checking the fan."
        )
        status_form.find_element(By.TAG_NAME, "button").click()
        self.wait.until(EC.text_to_be_present_in_element(
            (By.TAG_NAME, "body"), "In Progress"
        ))

    def test_10_admin_dashboard_shows_assigned_officer(self):
        ticket_id = self.__class__.ticket_id
        self.assertIsNotNone(ticket_id, "Test 06 must create a ticket before this test.")
        self.logout()
        self.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self.wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, "body"), ticket_id))
        self.assertIn(self.officer_name, self.driver.page_source)


class StatusTestResult(unittest.TextTestResult):
    """Print only PASS or FAIL; exceptions and skips are reported as FAIL."""

    def _short_name(self, test):
        return test.id().split(".")[-1]

    def _print_status(self, status, test, detail=""):
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {self._short_name(test)}{suffix}", flush=True)
        time.sleep(0.5)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._print_status("PASS", test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._print_status("FAIL", test, "assertion failed")

    def addError(self, test, err):
        # Count unexpected exceptions as failures so output contains no ERROR status.
        super().addFailure(test, err)
        self._print_status("FAIL", test, "unexpected exception")

    def addSkip(self, test, reason):
        # Count skipped tests as failures so every case ends as PASS or FAIL.
        try:
            raise AssertionError(f"Test skipped: {reason}")
        except AssertionError:
            err = sys.exc_info()
        super().addFailure(test, err)
        self._print_status("FAIL", test, f"would otherwise be skipped: {reason}")


class StatusTestRunner(unittest.TextTestRunner):
    resultclass = StatusTestResult


if __name__ == "__main__":
    # Test methods run in numbered order. Each result is printed as PASS or FAIL only.
    unittest.main(testRunner=StatusTestRunner, verbosity=0)
