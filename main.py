from dotenv import load_dotenv

from app.bot import ReceiptBot
from app.config import load_settings



def main() -> None:
    load_dotenv()
    settings = load_settings()
    bot = ReceiptBot(settings)
    bot.run()


if __name__ == "__main__":
    main()
