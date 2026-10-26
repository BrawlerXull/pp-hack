-- The official image creates POSTGRES_DB (checkout_guard) itself; the test suite needs its own database.
CREATE DATABASE checkout_guard_test;
