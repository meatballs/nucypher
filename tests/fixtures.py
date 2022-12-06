import contextlib
import json
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Callable, Tuple

import maya
import pytest
from click.testing import CliRunner
from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

import nucypher
import tests
from nucypher.blockchain.economics import Economics
from nucypher.blockchain.eth.actors import Operator
from nucypher.blockchain.eth.agents import (
    ContractAgency,
    NucypherTokenAgent,
    PREApplicationAgent,
)
from nucypher.blockchain.eth.deployers import (
    NucypherTokenDeployer,
    PREApplicationDeployer,
    SubscriptionManagerDeployer,
)
from nucypher.blockchain.eth.interfaces import BlockchainInterfaceFactory
from nucypher.blockchain.eth.registry import (
    InMemoryContractRegistry,
    LocalContractRegistry,
)
from nucypher.blockchain.eth.signers.software import KeystoreSigner, Web3Signer
from nucypher.characters.lawful import Enrico, Ursula
from nucypher.config.base import CharacterConfiguration
from nucypher.config.characters import (
    AliceConfiguration,
    BobConfiguration,
    UrsulaConfiguration,
)
from nucypher.config.constants import TEMPORARY_DOMAIN
from nucypher.crypto.keystore import Keystore
from nucypher.crypto.powers import CryptoPower, TransactingPower
from nucypher.network.nodes import TEACHER_NODES
from nucypher.policy.conditions.context import USER_ADDRESS_CONTEXT
from nucypher.policy.conditions.evm import ContractCondition, RPCCondition
from nucypher.policy.conditions.lingo import ReturnValueTest
from nucypher.policy.conditions.time import TimeCondition
from nucypher.policy.payment import SubscriptionManagerPayment
from nucypher.utilities.emitters import StdoutEmitter
from nucypher.utilities.logging import GlobalLoggerSettings, Logger
from nucypher.utilities.networking import LOOPBACK_ADDRESS
from tests.constants import (
    BASE_TEMP_DIR,
    BASE_TEMP_PREFIX,
    BONUS_TOKENS_FOR_TESTS,
    DATETIME_FORMAT,
    DEVELOPMENT_ETH_AIRDROP_AMOUNT,
    INSECURE_DEVELOPMENT_PASSWORD,
    MIN_STAKE_FOR_TESTS,
    MOCK_CUSTOM_INSTALLATION_PATH,
    MOCK_CUSTOM_INSTALLATION_PATH_2,
    MOCK_ETH_PROVIDER_URI,
    MOCK_IP_ADDRESS,
    MOCK_REGISTRY_FILEPATH,
    TEST_ETH_PROVIDER_URI,
    TEST_GAS_LIMIT,
    TESTERCHAIN_CHAIN_ID, NUMBER_OF_ETH_TEST_ACCOUNTS,
)
from tests.mock.interfaces import MockBlockchain, mock_registry_source_manager
from tests.mock.performance_mocks import (
    mock_cert_generation,
    mock_cert_loading,
    mock_cert_storage,
    mock_keep_learning,
    mock_message_verification,
    mock_record_fleet_state,
    mock_remember_node,
    mock_rest_app_creation,
    mock_verify_node,
)
from tests.utils.blockchain import TesterBlockchain, token_airdrop
from tests.utils.config import (
    make_alice_test_configuration,
    make_bob_test_configuration,
    make_ursula_test_configuration,
)
from tests.utils.middleware import (
    MockRestMiddleware,
    MockRestMiddlewareForLargeFleetTests,
)
from tests.utils.policy import generate_random_label
from tests.utils.ursula import MOCK_KNOWN_URSULAS_CACHE, make_ursulas, select_test_port

test_logger = Logger("test-logger")

# defer.setDebugging(True)

#
# Temporary
#


@pytest.fixture(scope="function")
def tempfile_path():
    fd, path = tempfile.mkstemp()
    path = Path(path)
    yield path
    os.close(fd)
    path.unlink()


@pytest.fixture(scope="module")
def temp_dir_path():
    temp_dir = tempfile.TemporaryDirectory(prefix='nucypher-test-')
    yield Path(temp_dir.name)
    temp_dir.cleanup()


@pytest.fixture(scope='function')
def certificates_tempdir():
    custom_filepath = '/tmp/nucypher-test-certificates-'
    cert_tmpdir = tempfile.TemporaryDirectory(prefix=custom_filepath)
    yield Path(cert_tmpdir.name)
    cert_tmpdir.cleanup()

#
# Accounts
#


@pytest.fixture(scope="module")
def random_account():
    key = Account.create(extra_entropy="lamborghini mercy")
    account = Account.from_key(private_key=key.key)
    return account


@pytest.fixture(scope="module")
def random_address(random_account):
    return random_account.address

#
# Character Configurations
#


@pytest.fixture(scope="module")
def ursula_test_config(test_registry, temp_dir_path, testerchain):
    config = make_ursula_test_configuration(
        eth_provider_uri=TEST_ETH_PROVIDER_URI,
        payment_provider=TEST_ETH_PROVIDER_URI,
        test_registry=test_registry,
        rest_port=select_test_port(),
        operator_address=testerchain.ursulas_accounts.pop(),
    )
    yield config
    config.cleanup()
    for k in list(MOCK_KNOWN_URSULAS_CACHE.keys()):
        del MOCK_KNOWN_URSULAS_CACHE[k]


@pytest.fixture(scope="module")
def alice_test_config(ursulas, testerchain, test_registry):
    config = make_alice_test_configuration(
        eth_provider_uri=TEST_ETH_PROVIDER_URI,
        payment_provider=TEST_ETH_PROVIDER_URI,
        known_nodes=ursulas,
        checksum_address=testerchain.alice_account,
        test_registry=test_registry,
    )
    yield config
    config.cleanup()


@pytest.fixture(scope="module")
def bob_test_config(testerchain, test_registry):
    config = make_bob_test_configuration(eth_provider_uri=TEST_ETH_PROVIDER_URI,
                                         test_registry=test_registry,
                                         checksum_address=testerchain.bob_account)
    yield config
    config.cleanup()


#
# Policies
#


@pytest.fixture(scope="module")
def idle_policy(testerchain, alice, bob, application_economics):
    """Creates a Policy, in a manner typical of how Alice might do it, with a unique label"""
    random_label = generate_random_label()
    expiration = maya.now() + timedelta(days=1)
    threshold, shares = 3, 5
    price = alice.payment_method.quote(
        expiration=expiration.epoch, shares=shares
    ).value  # TODO: use default quote option
    policy = alice.create_policy(
        bob,
        label=random_label,
        value=price,
        threshold=threshold,
        shares=shares,
        expiration=expiration,
    )
    return policy


@pytest.fixture(scope="module")
def enacted_policy(idle_policy, ursulas):
    # Alice has a policy in mind and knows of enough qualified Ursulas; she crafts an offer for them.

    # value and expiration were set when creating idle_policy already
    # cannot set them again
    # deposit = NON_PAYMENT(b"0000000")
    # contract_end_datetime = maya.now() + datetime.timedelta(days=5)
    network_middleware = MockRestMiddleware()

    # REST call happens here, as does population of TreasureMap.
    enacted_policy = idle_policy.enact(
        network_middleware=network_middleware, ursulas=list(ursulas)
    )
    return enacted_policy


@pytest.fixture(scope="module")
def treasure_map(enacted_policy, bob):
    """
    The unencrypted treasure map corresponding to the one in `enacted_policy`
    """
    yield bob._decrypt_treasure_map(
        enacted_policy.treasure_map, enacted_policy.publisher_verifying_key
    )


@pytest.fixture(scope="function")
def random_policy(testerchain, alice, bob, application_economics):
    random_label = generate_random_label()
    seconds = 60 * 60 * 24  # TODO This needs to be better thought out...?
    now = testerchain.w3.eth.get_block('latest').timestamp
    expiration = maya.MayaDT(now).add(seconds=seconds)
    shares = 3
    threshold = 2
    policy = alice.create_policy(
        bob,
        label=random_label,
        threshold=threshold,
        shares=shares,
        value=shares
        * seconds
        * 100,  # calculation probably needs to incorporate actual cost per second
        expiration=expiration,
    )
    return policy


@pytest.fixture(scope="module")
def capsule_side_channel(enacted_policy):
    class _CapsuleSideChannel:
        def __init__(self):
            self.enrico = Enrico(policy_encrypting_key=enacted_policy.public_key)
            self.messages = []
            self.plaintexts = []
            self.plaintext_passthrough = False

        def __call__(self):
            message = "Welcome to flippering number {}.".format(len(self.messages)).encode()
            message_kit = self.enrico.encrypt_message(message)
            self.messages.append((message_kit, self.enrico))
            if self.plaintext_passthrough:
                self.plaintexts.append(message)
            return message_kit

        def reset(self, plaintext_passthrough=False):
            self.enrico = Enrico(policy_encrypting_key=enacted_policy.public_key)
            self.messages.clear()
            self.plaintexts.clear()
            self.plaintext_passthrough = plaintext_passthrough
            return self(), self.enrico

    return _CapsuleSideChannel()


@pytest.fixture(scope="module")
def random_policy_label():
    yield generate_random_label()


#
# Alice, Bob, and Ursula
#

@pytest.fixture(scope="module")
def alice(alice_test_config, testerchain):
    alice = alice_test_config.produce()
    yield alice
    alice.disenchant()


@pytest.fixture(scope="module")
def bob(bob_test_config, testerchain):
    bob = bob_test_config.produce()
    yield bob
    bob.disenchant()


@pytest.fixture(scope="function")
def lonely_ursula_maker(ursula_test_config, testerchain):
    class _PartialUrsulaMaker:
        _partial = partial(
            make_ursulas,
            ursula_config=ursula_test_config,
            know_each_other=False,
            staking_provider_addresses=testerchain.stake_providers_accounts,
            operator_addresses=testerchain.ursulas_accounts,
        )
        _made = []

        def __call__(self, *args, **kwargs):
            ursulas = self._partial(*args, **kwargs)
            self._made.extend(ursulas)
            return ursulas

        def clean(self):
            for ursula in self._made:
                ursula.stop()
            for ursula in self._made:
                del MOCK_KNOWN_URSULAS_CACHE[ursula.rest_interface.port]
            for ursula in self._made:
                ursula._finalize()
    _maker = _PartialUrsulaMaker()
    yield _maker
    _maker.clean()


#
# Blockchain
#


@pytest.fixture(scope='module')
def application_economics():
    economics = Economics()
    return economics


@pytest.fixture(scope='module')
def test_registry():
    registry = InMemoryContractRegistry()
    return registry


def _make_testerchain(mock_backend: bool = False, population: int = NUMBER_OF_ETH_TEST_ACCOUNTS) -> TesterBlockchain:
    """
    https://github.com/ethereum/eth-tester     # available-backends
    """
    # Monkey patch to prevent gas adjustment
    import eth
    eth._utils.headers.GAS_LIMIT_MINIMUM = TEST_GAS_LIMIT
    eth._utils.headers.GENESIS_GAS_LIMIT = TEST_GAS_LIMIT
    eth.vm.forks.london.headers.GENESIS_GAS_LIMIT = TEST_GAS_LIMIT

    # Monkey patch to prevent gas estimates
    def _get_buffered_gas_estimate(web3, transaction, gas_buffer=100000):
        return TEST_GAS_LIMIT

    import web3
    web3.eth.get_buffered_gas_estimate = _get_buffered_gas_estimate

    # Create the blockchain
    if mock_backend:
        testerchain = MockBlockchain(population=population)
    else:
        testerchain = TesterBlockchain(eth_airdrop=True, free_transactions=True, test_accounts=population)

    return testerchain


@pytest.fixture(scope='session')
def _testerchain() -> TesterBlockchain:
    testerchain = _make_testerchain()
    yield testerchain


@pytest.fixture(scope='module')
def testerchain(_testerchain) -> TesterBlockchain:
    testerchain = _testerchain

    # Reset chain state
    pyevm_backend = testerchain.provider.ethereum_tester.backend
    snapshot = pyevm_backend.chain.get_canonical_block_by_number(0).hash
    pyevm_backend.revert_to_snapshot(snapshot)

    coinbase, *addresses = testerchain.client.accounts

    for address in addresses:
        balance = testerchain.client.get_balance(address)
        spent = DEVELOPMENT_ETH_AIRDROP_AMOUNT - balance

        if spent > 0:
            tx = {'to': address, 'from': coinbase, 'value': spent}
            txhash = testerchain.w3.eth.send_transaction(tx)

            _receipt = testerchain.wait_for_receipt(txhash)
            eth_amount = Web3().from_wei(spent, 'ether')
            testerchain.log.info("Airdropped {} ETH {} -> {}".format(eth_amount, tx['from'], tx['to']))

    BlockchainInterfaceFactory.register_interface(interface=testerchain, force=True)
    yield testerchain


@pytest.fixture(scope='module')
def _mock_testerchain() -> MockBlockchain:
    BlockchainInterfaceFactory._interfaces = dict()
    testerchain = _make_testerchain(mock_backend=True)
    BlockchainInterfaceFactory.register_interface(interface=testerchain)
    yield testerchain


@pytest.fixture(scope='module')
def _mock_testerchain_with_5000_ursulas() -> MockBlockchain:
    BlockchainInterfaceFactory._interfaces = dict()
    testerchain = _make_testerchain(mock_backend=True, population=30)
    BlockchainInterfaceFactory.register_interface(interface=testerchain)
    yield testerchain


@pytest.fixture(scope='module')
def deployer_transacting_power(testerchain):
    transacting_power = TransactingPower(password=INSECURE_DEVELOPMENT_PASSWORD,
                                         signer=Web3Signer(client=testerchain.client),
                                         account=testerchain.etherbase_account)
    transacting_power.unlock(password=INSECURE_DEVELOPMENT_PASSWORD)
    return transacting_power


def _make_agency(test_registry, token_economics, deployer_transacting_power, threshold_staking):
    transacting_power = deployer_transacting_power

    token_deployer = NucypherTokenDeployer(economics=token_economics, registry=test_registry)
    token_deployer.deploy(transacting_power=transacting_power)

    pre_application_deployer = PREApplicationDeployer(economics=token_economics,
                                                      registry=test_registry,
                                                      staking_interface=threshold_staking.address)
    pre_application_deployer.deploy(transacting_power=transacting_power)

    subscription_manager_deployer = SubscriptionManagerDeployer(economics=token_economics, registry=test_registry)
    subscription_manager_deployer.deploy(transacting_power=transacting_power)


@pytest.fixture(scope='module')
def test_registry_source_manager(test_registry):
    with mock_registry_source_manager(test_registry=test_registry):
        yield


@pytest.fixture(scope='module')
def agency(test_registry,
           application_economics,
           test_registry_source_manager,
           deployer_transacting_power,
           threshold_staking):
    _make_agency(test_registry=test_registry,
                 token_economics=application_economics,
                 deployer_transacting_power=deployer_transacting_power,
                 threshold_staking=threshold_staking)


@pytest.fixture(scope='module')
def agency_local_registry(testerchain, agency, test_registry):
    registry = LocalContractRegistry(filepath=MOCK_REGISTRY_FILEPATH)
    registry.write(test_registry.read())
    yield registry
    if MOCK_REGISTRY_FILEPATH.exists():
        MOCK_REGISTRY_FILEPATH.unlink()


@pytest.fixture(scope='module')
def threshold_staking(deploy_contract):
    threshold_staking, _ = deploy_contract('ThresholdStakingForPREApplicationMock')
    yield threshold_staking


@pytest.fixture(scope="module")
def staking_providers(testerchain, agency, test_registry, threshold_staking):
    pre_application_agent = ContractAgency.get_agent(PREApplicationAgent, registry=test_registry)
    blockchain = pre_application_agent.blockchain

    staking_providers = list()
    for provider_address, operator_address in zip(blockchain.stake_providers_accounts, blockchain.ursulas_accounts):
        provider_power = TransactingPower(account=provider_address, signer=Web3Signer(testerchain.client))
        provider_power.unlock(password=INSECURE_DEVELOPMENT_PASSWORD)

        # for a random amount
        amount = MIN_STAKE_FOR_TESTS + random.randrange(BONUS_TOKENS_FOR_TESTS)

        # initialize threshold stake
        tx = threshold_staking.functions.setRoles(provider_address).transact()
        testerchain.wait_for_receipt(tx)
        tx = threshold_staking.functions.setStakes(provider_address, amount, 0, 0).transact()
        testerchain.wait_for_receipt(tx)

        # We assume that the staking provider knows in advance the account of her operator
        pre_application_agent.bond_operator(staking_provider=provider_address,
                                            operator=operator_address,
                                            transacting_power=provider_power)

        operator_power = TransactingPower(
            account=operator_address, signer=Web3Signer(testerchain.client)
        )
        operator = Operator(
            is_me=True,
            operator_address=operator_address,
            domain=TEMPORARY_DOMAIN,
            registry=test_registry,
            transacting_power=operator_power,
            eth_provider_uri=testerchain.eth_provider_uri,
            signer=Web3Signer(testerchain.client),
            crypto_power=CryptoPower(power_ups=[operator_power]),
            payment_method=SubscriptionManagerPayment(
                eth_provider=testerchain.eth_provider_uri,
                network=TEMPORARY_DOMAIN,
                registry=test_registry,
            ),
        )
        operator.confirm_address()  # assume we always need a "pre-confirmed" operator for now.

        # track
        staking_providers.append(provider_address)

    yield staking_providers


@pytest.fixture()
def light_ursula(temp_dir_path, test_registry_source_manager, random_account, mocker):
    mocker.patch.object(
        KeystoreSigner, "_KeystoreSigner__get_signer", return_value=random_account
    )
    payment_method = SubscriptionManagerPayment(
        eth_provider=MOCK_ETH_PROVIDER_URI, network=TEMPORARY_DOMAIN
    )
    ursula = Ursula(
        rest_host=LOOPBACK_ADDRESS,
        rest_port=select_test_port(),
        domain=TEMPORARY_DOMAIN,
        payment_method=payment_method,
        checksum_address=random_account.address,
        operator_address=random_account.address,
        eth_provider_uri=MOCK_ETH_PROVIDER_URI,
        signer=KeystoreSigner(path=temp_dir_path),
    )
    return ursula


@pytest.fixture(scope="module")
def ursulas(testerchain, staking_providers, ursula_test_config):
    if MOCK_KNOWN_URSULAS_CACHE:
        # TODO: Is this a safe assumption / test behaviour?
        # raise RuntimeError("Ursulas cache was unclear at fixture loading time.  Did you use one of the ursula maker functions without cleaning up?")
        MOCK_KNOWN_URSULAS_CACHE.clear()

    _ursulas = make_ursulas(
        ursula_config=ursula_test_config,
        staking_provider_addresses=testerchain.stake_providers_accounts,
        operator_addresses=testerchain.ursulas_accounts,
        know_each_other=True,
    )
    for u in _ursulas:
        u.synchronous_query_timeout = .01  # We expect to never have to wait for content that is actually on-chain during tests.

    _ports_to_remove = [ursula.rest_interface.port for ursula in _ursulas]
    yield _ursulas

    for port in _ports_to_remove:
        del MOCK_KNOWN_URSULAS_CACHE[port]

    for u in _ursulas:
        u.stop()
        u._finalize()

    # Pytest will hold on to this object, need to clear it manually.
    # See https://github.com/pytest-dev/pytest/issues/5642
    _ursulas.clear()


@pytest.fixture(scope='module')
def policy_rate():
    rate = Web3.to_wei(21, 'gwei')
    return rate


@pytest.fixture(scope='module')
def policy_value(application_economics, policy_rate):
    value = policy_rate * application_economics.min_operator_seconds
    return value


@pytest.fixture(scope='module')
def funded_blockchain(testerchain, agency, application_economics, test_registry):
    # Who are ya'?
    deployer_address, *everyone_else, staking_participant = testerchain.client.accounts

    transacting_power = TransactingPower(account=testerchain.etherbase_account,
                                         signer=Web3Signer(testerchain.client))

    # Free ETH!!!
    testerchain.ether_airdrop(amount=DEVELOPMENT_ETH_AIRDROP_AMOUNT)

    # Free Tokens!!!
    token_airdrop(token_agent=NucypherTokenAgent(registry=test_registry),
                  transacting_power=transacting_power,
                  addresses=everyone_else,
                  amount=application_economics.min_authorization * 5)

    # HERE YOU GO
    yield testerchain, deployer_address


@pytest.fixture(scope='module')
def manual_operator(testerchain):
    worker_private_key = os.urandom(32).hex()
    address = testerchain.provider.ethereum_tester.add_account(worker_private_key,
                                                               password=INSECURE_DEVELOPMENT_PASSWORD)

    tx = {'to': address,
          'from': testerchain.etherbase_account,
          'value': Web3.to_wei('1', 'ether')}

    txhash = testerchain.client.w3.eth.send_transaction(tx)
    _receipt = testerchain.wait_for_receipt(txhash)
    yield address


#
# Test logging
#

# TODO : Use a pytest Flag to enable/disable this functionality
test_logger = Logger("test-logger")


@pytest.fixture(autouse=True, scope='function')
def log_in_and_out_of_test(request):
    test_name = request.node.name
    module_name = request.module.__name__

    test_logger.info(f"Starting {module_name}.py::{test_name}")
    yield
    test_logger.info(f"Finalized {module_name}.py::{test_name}")


@pytest.fixture(scope="module")
def deploy_contract(testerchain, test_registry) -> Callable[..., Tuple[Contract, TxReceipt]]:
    def wrapped(contract_name, *args, **kwargs):
        tpower = TransactingPower(account=testerchain.etherbase_account,
                                  signer=Web3Signer(testerchain.client))
        return testerchain.deploy_contract(tpower,
                                           test_registry,
                                           contract_name,
                                           *args,
                                           **kwargs)
    return wrapped


@pytest.fixture(scope='module')
def get_random_checksum_address():
    def _get_random_checksum_address():
        canonical_address = os.urandom(20)
        checksum_address = to_checksum_address(canonical_address)
        return checksum_address

    return _get_random_checksum_address


@pytest.fixture(scope="module")
def fleet_of_highperf_mocked_ursulas(ursula_test_config, request, big_testerchain):

    mocks = (
        mock_cert_storage,
        mock_cert_loading,
        mock_rest_app_creation,
        mock_cert_generation,
        mock_remember_node,
        mock_message_verification,
        )

    try:
        quantity = request.param
    except AttributeError:
        quantity = 5000  # Bigass fleet by default; that's kinda the point.

    staking_addresses = (to_checksum_address('0x' + os.urandom(20).hex()) for _ in range(5000))
    operator_addresses = (to_checksum_address('0x' + os.urandom(20).hex()) for _ in range(5000))

    with GlobalLoggerSettings.pause_all_logging_while():
        with contextlib.ExitStack() as stack:

            for mock in mocks:
                stack.enter_context(mock)

            _ursulas = make_ursulas(
                ursula_config=ursula_test_config,
                max_quantity=quantity,
                know_each_other=False,
                staking_provider_addresses=staking_addresses,
                operator_addresses=operator_addresses,
            )
            all_ursulas = {u.checksum_address: u for u in _ursulas}

            for ursula in _ursulas:
                # FIXME #2588: FleetSensor should not own fully-functional Ursulas.
                # It only needs to see whatever public info we can normally get via REST.
                # Also sharing mutable Ursulas like that can lead to unpredictable results.
                ursula.known_nodes.current_state._nodes = all_ursulas
                ursula.known_nodes.current_state.checksum = b"This is a fleet state checksum..".hex()

    yield _ursulas

    for ursula in _ursulas:
        del MOCK_KNOWN_URSULAS_CACHE[ursula.rest_interface.port]


@pytest.fixture(scope="module")
def highperf_mocked_alice(fleet_of_highperf_mocked_ursulas, test_registry_source_manager, monkeymodule, testerchain):
    monkeymodule.setattr(CharacterConfiguration, 'DEFAULT_PAYMENT_NETWORK', TEMPORARY_DOMAIN)

    config = AliceConfiguration(dev_mode=True,
                                domain=TEMPORARY_DOMAIN,
                                checksum_address=testerchain.alice_account,
                                network_middleware=MockRestMiddlewareForLargeFleetTests(),
                                abort_on_learning_error=True,
                                save_metadata=False,
                                reload_metadata=False)

    with mock_cert_storage, mock_verify_node, mock_message_verification, mock_keep_learning:
        alice = config.produce(known_nodes=list(fleet_of_highperf_mocked_ursulas)[:1])
    yield alice
    # TODO: Where does this really, truly belong?
    alice._learning_task.stop()


@pytest.fixture(scope="module")
def highperf_mocked_bob(fleet_of_highperf_mocked_ursulas):
    config = BobConfiguration(dev_mode=True,
                              domain=TEMPORARY_DOMAIN,
                              network_middleware=MockRestMiddlewareForLargeFleetTests(),
                              abort_on_learning_error=True,
                              save_metadata=False,
                              reload_metadata=False)

    with mock_cert_storage, mock_verify_node, mock_record_fleet_state, mock_keep_learning:
        bob = config.produce(known_nodes=list(fleet_of_highperf_mocked_ursulas)[:1])
    yield bob
    bob._learning_task.stop()
    return bob


#
# CLI
#


@pytest.fixture(scope='function')
def test_emitter(mocker):
    # Note that this fixture does not capture console output.
    # Whether the output is captured or not is controlled by
    # the usage of the (built-in) `capsys` fixture or global PyTest run settings.
    return StdoutEmitter()


@pytest.fixture(scope='module')
def click_runner():
    runner = CliRunner()
    yield runner


@pytest.fixture(scope='module')
def nominal_configuration_fields(test_registry_source_manager):
    config = UrsulaConfiguration(dev_mode=True, payment_network=TEMPORARY_DOMAIN, domain=TEMPORARY_DOMAIN)
    config_fields = config.static_payload()
    yield tuple(config_fields.keys())
    del config


@pytest.fixture(scope='function')
def new_local_registry():
    filename = f'{BASE_TEMP_PREFIX}mock-empty-registry-{datetime.now().strftime(DATETIME_FORMAT)}.json'
    registry_filepath = BASE_TEMP_DIR / filename
    registry = LocalContractRegistry(filepath=registry_filepath)
    registry.write(InMemoryContractRegistry().read())
    yield registry
    if registry_filepath.exists():
        registry_filepath.unlink()


@pytest.fixture(scope='module')
def custom_filepath():
    _custom_filepath = MOCK_CUSTOM_INSTALLATION_PATH
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(_custom_filepath, ignore_errors=True)
    yield _custom_filepath
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(_custom_filepath, ignore_errors=True)


@pytest.fixture(scope='module')
def custom_filepath_2():
    _custom_filepath = MOCK_CUSTOM_INSTALLATION_PATH_2
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(_custom_filepath, ignore_errors=True)
    try:
        yield _custom_filepath
    finally:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(_custom_filepath, ignore_errors=True)


@pytest.fixture(scope='module')
def worker_configuration_file_location(custom_filepath) -> Path:
    _configuration_file_location = MOCK_CUSTOM_INSTALLATION_PATH / UrsulaConfiguration.generate_filename()
    return _configuration_file_location


@pytest.fixture(autouse=True)
def mock_teacher_nodes(mocker):
    mock_nodes = tuple(u.rest_url() for u in MOCK_KNOWN_URSULAS_CACHE.values())[0:2]
    mocker.patch.dict(TEACHER_NODES, {TEMPORARY_DOMAIN: mock_nodes}, clear=True)


@pytest.fixture(autouse=True)
def disable_interactive_keystore_generation(mocker):
    # Do not notify or confirm mnemonic seed words during tests normally
    mocker.patch.object(Keystore, '_confirm_generate')


#
# Web Auth
#
@pytest.fixture(scope='module')
def basic_auth_file(temp_dir_path):
    basic_auth = Path(temp_dir_path) / 'htpasswd'
    with basic_auth.open("w") as f:
        # username: "admin", password: "admin"
        f.write("admin:$apr1$hlEpWVoI$0qjykXrvdZ0yO2TnBggQO0\n")
    yield basic_auth
    basic_auth.unlink()


@pytest.fixture(scope='module')
def mock_rest_middleware():
    return MockRestMiddleware()


#
# Conditions
#


@pytest.fixture(scope='session')
def conditions_test_data():
    test_conditions = Path(tests.__file__).parent / "data" / "test_conditions.json"
    with open(test_conditions, 'r') as file:
        data = json.loads(file.read())
    for name, condition in data.items():
        if condition.get('chain'):
            condition['chain'] = TESTERCHAIN_CHAIN_ID
    return data


@pytest.fixture(autouse=True)
def mock_condition_blockchains(mocker):
    """adds testerchain's chain ID to permitted conditional chains"""
    mocker.patch.object(
        nucypher.policy.conditions.evm, "_CONDITION_CHAINS", tuple([TESTERCHAIN_CHAIN_ID])
    )


@pytest.fixture
def timelock_condition():
    condition = TimeCondition(
        return_value_test=ReturnValueTest('>', 0)
    )
    return condition


@pytest.fixture
def compound_timelock_lingo():
    return [
        {'returnValueTest': {'value': '0', 'comparator': '>'}, 'method': 'timelock'},
        {'operator': 'and'},
        {'returnValueTest': {'value': '99999999999999999', 'comparator': '<'}, 'method': 'timelock'},
        {'operator': 'and'},
        {'returnValueTest': {'value': '0', 'comparator': '>'}, 'method': 'timelock'}
    ]


@pytest.fixture
def rpc_condition():
    condition = RPCCondition(
        method="eth_getBalance",
        chain=TESTERCHAIN_CHAIN_ID,
        return_value_test=ReturnValueTest("==", Web3.to_wei(1_000_000, "ether")),
        parameters=[USER_ADDRESS_CONTEXT],
    )
    return condition


@pytest.fixture(scope='module')
def valid_user_address_context():
    return {
        USER_ADDRESS_CONTEXT: {
            "signature": "0x488a7acefdc6d098eedf73cdfd379777c0f4a4023a660d350d3bf309a51dd4251abaad9cdd11b71c400cfb4625c14ca142f72b39165bd980c8da1ea32892ff071c",
            "address": "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E",
            "typedData": {
                "primaryType": "Wallet",
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "salt", "type": "bytes32"},
                    ],
                    "Wallet": [
                        {"name": "address", "type": "string"},
                        {"name": "blockNumber", "type": "uint256"},
                        {"name": "blockHash", "type": "bytes32"},
                        {"name": "signatureText", "type": "string"},
                    ],
                },
                "domain": {
                    "name": "tDec",
                    "version": "1",
                    "chainId": 80001,
                    "salt": "0x3e6365d35fd4e53cbc00b080b0742b88f8b735352ea54c0534ed6a2e44a83ff0",
                },
                "message": {
                    "address": "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E",
                    "blockNumber": 28117088,
                    "blockHash": "0x104dfae58be4a9b15d59ce447a565302d5658914f1093f10290cd846fbe258b7",
                    "signatureText": "I'm the owner of address 0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E as of block number 28117088",
                },
            },
        }
    }
