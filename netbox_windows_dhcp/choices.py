from utilities.choices import ChoiceSet


class SyncQueueChoices(ChoiceSet):
    HIGH = 'high'
    DEFAULT = 'default'
    LOW = 'low'

    CHOICES = [
        (HIGH, 'High'),
        (DEFAULT, 'Default'),
        (LOW, 'Low'),
    ]


class SyncLogLevelChoices(ChoiceSet):
    DEBUG = 'DEBUG'
    INFO = 'INFO'
    WARNING = 'WARNING'
    ERROR = 'ERROR'

    CHOICES = [
        (DEBUG, 'Debug'),
        (INFO, 'Info'),
        (WARNING, 'Warning'),
        (ERROR, 'Error'),
    ]


class DHCPServerHealthChoices(ChoiceSet):
    UNKNOWN = 'unknown'
    HEALTHY = 'healthy'
    UNREACHABLE = 'unreachable'

    CHOICES = [
        (UNKNOWN, 'Unknown', 'gray'),
        (HEALTHY, 'Healthy', 'green'),
        (UNREACHABLE, 'Unreachable', 'red'),
    ]


class DHCPServerAccessChoices(ChoiceSet):
    UNKNOWN = 'unknown'
    RO = 'ro'
    RW = 'rw'

    CHOICES = [
        (UNKNOWN, 'Unknown', 'gray'),
        (RO, 'Read-Only', 'blue'),
        (RW, 'Read-Write', 'green'),
    ]


class DHCPFailoverModeChoices(ChoiceSet):
    LOAD_BALANCE = 'LoadBalance'
    HOT_STANDBY = 'HotStandby'

    CHOICES = [
        (LOAD_BALANCE, 'Load Balance'),
        (HOT_STANDBY, 'Hot Standby'),
    ]


class DHCPOptionDataTypeChoices(ChoiceSet):
    STRING = 'String'
    IP_ADDRESS = 'IPAddress'
    IP_ADDRESS_LIST = 'IPAddressList'
    DWORD = 'DWORD'
    DWORD_DWORD = 'DWORD DWORD'
    BINARY = 'Binary'
    ENCAPSULATED = 'Encapsulated'
    IPV6_ADDRESS = 'IPv6Address'

    CHOICES = [
        (STRING, 'String'),
        (IP_ADDRESS, 'IP Address'),
        (IP_ADDRESS_LIST, 'IP Address List'),
        (DWORD, 'DWORD (32-bit)'),
        (DWORD_DWORD, 'DWORD DWORD (64-bit)'),
        (BINARY, 'Binary'),
        (ENCAPSULATED, 'Encapsulated'),
        (IPV6_ADDRESS, 'IPv6 Address'),
    ]
