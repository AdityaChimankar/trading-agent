"""Explicit lifecycle commands; no training, paper writes or promotion on import."""
import argparse
import json
import time


def main():
    parser = argparse.ArgumentParser(description="BUY/SELL outcome model lifecycle (research only)")
    commands = parser.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train")
    train.add_argument("symbols", nargs="+")
    paper = commands.add_parser("paper")
    paper.add_argument("candidate_id")
    paper.add_argument("--watch", action="store_true", help="Observe every 10 seconds until Ctrl+C; run during market hours")
    promote = commands.add_parser("promote")
    promote.add_argument("candidate_id")
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "train":
            from trading_agent.strategy.train_outcome_models import train_candidate
            result = train_candidate(args.symbols)
        elif args.command == "paper":
            from trading_agent.strategy.outcome_paper import observe
            while True:
                result = observe(args.candidate_id)
                print(json.dumps(result, indent=2), flush=True)
                if not args.watch:
                    return
                time.sleep(10)
        elif args.command == "promote":
            from trading_agent.strategy.outcome_paper import promote
            result = promote(args.candidate_id)
        else:
            from trading_agent.strategy.outcome_model import candidate_status
            result = candidate_status()
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        print("Paper observer stopped; recorded evidence preserved.")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
