"""explicit opt-in for tests that read a real dvd image."""


def pytest_addoption(parser):
    parser.addoption(
        "--dvd-iso", metavar="path", help="scan this dvd image and encode a five-second sample"
    )
