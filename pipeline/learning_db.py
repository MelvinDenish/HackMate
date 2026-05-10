"""
╔══════════════════════════════════════════════════════════════╗
║  LEARNING DATABASE — Cross-Run Intelligence                  ║
║                                                              ║
║  Inspired by: Devin (learns from repo patterns)              ║
║                                                              ║
║  A lightweight SQLite database that persists across runs.    ║
║  Records outcomes (what worked, what failed, what cost $)    ║
║  so the pipeline gets smarter over time.                     ║
║                                                              ║
║  Use cases:                                                  ║
║  1. "What tech stack works best for CRUD apps?"              ║
║  2. "What security issues keep appearing?"                   ║
║  3. "What's the average cost per run?"                       ║
║  4. "Which tasks fail most often?"                           ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".hackmate" / "learning.db"


class LearningDB:
    """Persistent cross-run learning database for pipeline intelligence."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

        logger.info(f"[LearningDB] Connected: {self.db_path}")

    def _create_tables(self):
        """Create database tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                problem_statement TEXT,
                problem_type TEXT,
                tech_stack TEXT,
                duration_seconds REAL,
                total_cost_usd REAL,
                total_tasks INTEGER,
                review_passed_first BOOLEAN,
                review_iterations INTEGER,
                security_verdict TEXT,
                security_issues_count INTEGER,
                deployment_success BOOLEAN,
                deployment_url TEXT,
                outcome TEXT DEFAULT 'unknown'
            );

            CREATE TABLE IF NOT EXISTS task_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                task_id TEXT,
                task_title TEXT,
                role TEXT,
                complexity TEXT,
                model_used TEXT,
                files_count INTEGER,
                syntax_errors_found BOOLEAN,
                self_corrected BOOLEAN,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS security_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                finding_type TEXT,
                severity TEXT,
                file_pattern TEXT,
                description TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS stack_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_type TEXT,
                tech_stack TEXT,
                score REAL,
                notes TEXT,
                timestamp TEXT
            );
        """)
        self._conn.commit()

    def record_run(
        self,
        problem_statement: str,
        problem_type: str = "",
        tech_stack: str = "",
        duration_seconds: float = 0,
        total_cost_usd: float = 0,
        total_tasks: int = 0,
        review_passed_first: bool = False,
        review_iterations: int = 0,
        security_verdict: str = "",
        security_issues_count: int = 0,
        deployment_success: bool = False,
        deployment_url: str = "",
    ) -> int:
        """Record a pipeline run. Returns the run ID."""
        cursor = self._conn.execute(
            """INSERT INTO runs
            (timestamp, problem_statement, problem_type, tech_stack,
             duration_seconds, total_cost_usd, total_tasks,
             review_passed_first, review_iterations,
             security_verdict, security_issues_count,
             deployment_success, deployment_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                problem_statement[:500],
                problem_type,
                tech_stack,
                duration_seconds,
                total_cost_usd,
                total_tasks,
                review_passed_first,
                review_iterations,
                security_verdict,
                security_issues_count,
                deployment_success,
                deployment_url,
            )
        )
        self._conn.commit()
        run_id = cursor.lastrowid
        logger.info(f"[LearningDB] Recorded run #{run_id}")
        return run_id

    def record_task_outcome(
        self,
        run_id: int,
        task_id: str,
        task_title: str,
        role: str = "",
        complexity: str = "",
        model_used: str = "",
        files_count: int = 0,
        syntax_errors_found: bool = False,
        self_corrected: bool = False,
    ) -> None:
        """Record outcome for a single task within a run."""
        self._conn.execute(
            """INSERT INTO task_outcomes
            (run_id, task_id, task_title, role, complexity,
             model_used, files_count, syntax_errors_found, self_corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, task_id, task_title, role, complexity,
             model_used, files_count, syntax_errors_found, self_corrected)
        )
        self._conn.commit()

    def record_security_pattern(
        self,
        run_id: int,
        finding_type: str,
        severity: str,
        file_pattern: str = "",
        description: str = "",
    ) -> None:
        """Record a security finding for cross-run pattern detection."""
        self._conn.execute(
            """INSERT INTO security_patterns
            (run_id, finding_type, severity, file_pattern, description)
            VALUES (?, ?, ?, ?, ?)""",
            (run_id, finding_type, severity, file_pattern, description)
        )
        self._conn.commit()

    def get_best_stack(self, problem_type: str) -> Optional[dict]:
        """Query: what tech stack worked best for this problem type?"""
        row = self._conn.execute(
            """SELECT tech_stack, AVG(total_cost_usd) as avg_cost,
                      COUNT(*) as run_count,
                      AVG(review_iterations) as avg_iterations,
                      SUM(deployment_success) as deploy_successes
               FROM runs
               WHERE problem_type LIKE ?
               GROUP BY tech_stack
               ORDER BY deploy_successes DESC, avg_iterations ASC
               LIMIT 1""",
            (f"%{problem_type}%",)
        ).fetchone()

        if row:
            return dict(row)
        return None

    def get_common_security_issues(self, limit: int = 10) -> list[dict]:
        """Query: what security issues appear most often across runs?"""
        rows = self._conn.execute(
            """SELECT finding_type, severity, description,
                      COUNT(*) as frequency
               FROM security_patterns
               GROUP BY finding_type
               ORDER BY frequency DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()

        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get aggregate statistics across all runs."""
        row = self._conn.execute(
            """SELECT COUNT(*) as total_runs,
                      AVG(total_cost_usd) as avg_cost,
                      AVG(duration_seconds) as avg_duration,
                      AVG(review_iterations) as avg_review_iterations,
                      SUM(deployment_success) * 100.0 / MAX(COUNT(*), 1) as deploy_rate
               FROM runs"""
        ).fetchone()

        return dict(row) if row else {}

    def get_recent_runs(self, limit: int = 5) -> list[dict]:
        """Get most recent pipeline runs."""
        rows = self._conn.execute(
            """SELECT * FROM runs ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()

        return [dict(r) for r in rows]

    def get_insights(self, tech_stack: str = "") -> str:
        """Generate actionable insights from historical runs.

        Returns a formatted string suitable for injection into agent prompts.
        This is the READ side of the cross-run learning flywheel.
        """
        insights = []

        # 1. Overall stats
        stats = self.get_stats()
        total_runs = stats.get("total_runs", 0)
        if total_runs == 0:
            return "No historical data available (first run)."

        insights.append(f"Based on {total_runs} previous pipeline runs:")

        # 2. Average cost and duration
        avg_cost = stats.get("avg_cost", 0)
        avg_dur = stats.get("avg_duration", 0)
        if avg_cost:
            insights.append(f"- Average cost: ${avg_cost:.2f} per run")
        if avg_dur:
            insights.append(f"- Average duration: {avg_dur/60:.1f} minutes")

        # 3. Review pass rate
        avg_iters = stats.get("avg_review_iterations", 0)
        if avg_iters:
            if avg_iters > 1.5:
                insights.append(
                    f"- ⚠️ Average review iterations: {avg_iters:.1f} "
                    f"(aim for <1.5 — write more defensive code)"
                )
            else:
                insights.append(f"- ✅ Review iterations: {avg_iters:.1f} (good)")

        # 4. Stack-specific insights
        if tech_stack:
            stack_row = self._conn.execute(
                """SELECT COUNT(*) as runs,
                          AVG(review_iterations) as avg_iters,
                          AVG(total_cost_usd) as avg_cost,
                          SUM(deployment_success) * 100.0 / MAX(COUNT(*), 1) as deploy_rate
                   FROM runs WHERE tech_stack LIKE ?""",
                (f"%{tech_stack}%",)
            ).fetchone()

            if stack_row and dict(stack_row).get("runs", 0) > 0:
                sr = dict(stack_row)
                insights.append(f"\nFor '{tech_stack}' projects ({sr['runs']} runs):")
                if sr.get("deploy_rate"):
                    insights.append(f"- Deploy success rate: {sr['deploy_rate']:.0f}%")
                if sr.get("avg_cost"):
                    insights.append(f"- Average cost: ${sr['avg_cost']:.2f}")

        # 5. Common security issues
        sec_issues = self.get_common_security_issues(5)
        if sec_issues:
            insights.append("\nCommon security issues to prevent:")
            for issue in sec_issues[:5]:
                insights.append(
                    f"- {issue['severity']}: {issue['finding_type']} "
                    f"(seen {issue['frequency']}x)"
                )

        # 6. Task complexity insights
        task_row = self._conn.execute(
            """SELECT role, complexity,
                      COUNT(*) as count,
                      AVG(syntax_errors_found) as error_rate
               FROM task_outcomes
               GROUP BY role, complexity
               ORDER BY error_rate DESC
               LIMIT 5"""
        ).fetchall()

        if task_row:
            high_error = [dict(r) for r in task_row if dict(r).get("error_rate", 0) > 0.3]
            if high_error:
                insights.append("\nHigh-error-rate task types (need extra care):")
                for t in high_error:
                    insights.append(
                        f"- {t['role']}/{t['complexity']}: "
                        f"{t['error_rate']*100:.0f}% error rate"
                    )

        return "\n".join(insights)

    def close(self):
        """Close the database connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
