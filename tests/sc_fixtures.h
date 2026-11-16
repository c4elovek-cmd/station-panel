#ifndef SOUNDYA_TESTS_SC_FIXTURES_H
#define SOUNDYA_TESTS_SC_FIXTURES_H

/* Автономные тесты sc.c без сети; см. sc_fixtures.c. */
void sc_fixture_run_search(void);
void sc_fixture_run_urn_and_likes(void);
void sc_fixture_run_skip_and_stub(void);
void sc_fixture_run_broken(void);
void sc_fixture_run_nested(void);
void sc_fixture_run_security(void);
void sc_fixture_run_limits(void);
int sc_fixture_summary(void);

#endif
