#!/usr/bin/env python3
"""
Perplexity Sonar Research Module for IDP Pipeline
Location: ~/repos/tools/pplx-research.py
Requires: pip install perplexityai
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from perplexity import Perplexity

TOOLS_DIR = Path(__file__).parent
CACHE_DIR = TOOLS_DIR / "research-cache"
SUPERPROMPT = (TOOLS_DIR / "research-superprompt.md").read_text()
USER_TEMPLATE = (TOOLS_DIR / "research-user-template.md").read_text()

client = Perplexity()  # reads PERPLEXITY_API_KEY from env


def deep_research(
    product_name: str, description: str, target_market: str, pricing_intent: str
) -> dict:
    """
    Full competitive/market/technical research via sonar-deep-research.
    Takes 5-10 min. Returns structured report + sources.
    Cost: ~$0.50-$1.00 per call.
    """
    user_prompt = (
        USER_TEMPLATE.replace("{{PRODUCT_NAME}}", product_name)
        .replace("{{ONE_PARAGRAPH_DESCRIPTION}}", description)
        .replace("{{WHO_BUYS_THIS}}", target_market)
        .replace("{{FREE_FREEMIUM_PAID_ENTERPRISE}}", pricing_intent)
    )

    start = time.time()
    completion = client.chat.completions.create(
        model="sonar-deep-research",
        messages=[
            {"role": "system", "content": SUPERPROMPT},
            {"role": "user", "content": user_prompt},
        ],
        web_search_options={"search_context_size": "high"},
        temperature=0.1,
    )
    elapsed = round(time.time() - start, 1)

    result = {
        "report": completion.choices[0].message.content,
        "citations": getattr(completion, "citations", []),
        "search_results": getattr(completion, "search_results", []),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "elapsed_seconds": elapsed,
        "timestamp": datetime.now(UTC).isoformat(),
        "product_name": product_name,
    }

    # Cache automatically
    _cache_result(product_name, "deep-research", result)
    return result


def targeted_research(question: str, domains: list = None, project_name: str = "general") -> dict:
    """
    Quick focused follow-up via sonar-pro.
    Use for RESEARCH: [angle] responses from founder.
    Cost: ~$0.01-$0.05 per call.
    """
    kwargs = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": SUPERPROMPT},
            {"role": "user", "content": question},
        ],
        "web_search_options": {"search_context_size": "medium"},
        "temperature": 0.1,
    }
    if domains:
        kwargs["search_domain_filter"] = domains

    completion = client.chat.completions.create(**kwargs)

    result = {
        "report": completion.choices[0].message.content,
        "citations": getattr(completion, "citations", []),
        "search_results": getattr(completion, "search_results", []),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "timestamp": datetime.now(UTC).isoformat(),
        "question": question,
    }

    _cache_result(project_name, f"followup-{int(time.time())}", result)
    return result


def github_research(query: str, project_name: str = "general") -> dict:
    """Search GitHub specifically for repos, libraries, frameworks."""
    return targeted_research(
        question=query, domains=["github.com", "npmjs.com", "pypi.org"], project_name=project_name
    )


def market_research(query: str, project_name: str = "general") -> dict:
    """Search business/market sources specifically."""
    return targeted_research(
        question=query,
        domains=[
            "crunchbase.com",
            "techcrunch.com",
            "pitchbook.com",
            "bloomberg.com",
            "reuters.com",
        ],
        project_name=project_name,
    )


def _cache_result(project_name: str, label: str, result: dict):
    """Save research to ~/repos/tools/research-cache/{project}/{label}.json + .md"""
    safe_name = project_name.lower().replace(" ", "-")
    project_dir = CACHE_DIR / safe_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Save full JSON (for programmatic use by Claude Code)
    json_path = project_dir / f"{label}.json"
    json_path.write_text(json.dumps(result, indent=2, default=str))

    # Save markdown report (for human review on Telegram)
    md_path = project_dir / f"{label}.md"
    header = f"# Research: {result.get('product_name', result.get('question', label))}\n"
    header += f"_Generated: {result['timestamp']}_\n\n"
    if result.get("usage"):
        cost = result["usage"].get("cost", {})
        if isinstance(cost, dict):
            header += f"_Cost: ${cost.get('total_cost', 'N/A')}_\n\n"
    header += "---\n\n"
    md_path.write_text(header + result["report"])

    # Save sources separately
    sources_path = project_dir / f"{label}-sources.json"
    sources_path.write_text(
        json.dumps(
            {
                "citations": result.get("citations", []),
                "search_results": result.get("search_results", []),
            },
            indent=2,
            default=str,
        )
    )


# === CLI interface for Claude Code invocation ===
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Perplexity Sonar Research")
    sub = parser.add_subparsers(dest="command")

    # Deep research
    deep = sub.add_parser("deep", help="Full competitive analysis")
    deep.add_argument("--name", required=True, help="Product name")
    deep.add_argument("--description", required=True, help="One paragraph description")
    deep.add_argument("--market", required=True, help="Target market")
    deep.add_argument("--pricing", required=True, help="Pricing intent")

    # Targeted follow-up
    follow = sub.add_parser("followup", help="Quick targeted research")
    follow.add_argument("--question", required=True, help="Research question")
    follow.add_argument("--project", default="general", help="Project name for caching")
    follow.add_argument("--domains", nargs="*", help="Domain filters")

    # GitHub-specific
    gh = sub.add_parser("github", help="GitHub repo research")
    gh.add_argument("--query", required=True, help="What to search for")
    gh.add_argument("--project", default="general", help="Project name")

    # Market-specific
    mkt = sub.add_parser("market", help="Market/business research")
    mkt.add_argument("--query", required=True, help="What to search for")
    mkt.add_argument("--project", default="general", help="Project name")

    args = parser.parse_args()

    if args.command == "deep":
        result = deep_research(args.name, args.description, args.market, args.pricing)
        print(
            f"\n✅ Deep research complete. Cost: ${result['usage'].get('cost', {}).get('total_cost', 'N/A')}"
        )
        print(f"⏱  Elapsed: {result['elapsed_seconds']}s")
        print(f"📁 Cached to: research-cache/{args.name.lower().replace(' ', '-')}/")

    elif args.command == "followup":
        result = targeted_research(args.question, args.domains, args.project)
        print("\n✅ Follow-up complete.")
        print(f"📁 Cached to: research-cache/{args.project}/")

    elif args.command == "github":
        result = github_research(args.query, args.project)
        print("\n✅ GitHub research complete.")

    elif args.command == "market":
        result = market_research(args.query, args.project)
        print("\n✅ Market research complete.")

    else:
        parser.print_help()
