import enum
import textwrap
from collections import defaultdict
from typing import Dict, List, Optional, Type

from uaclient import exceptions, system
from uaclient.config import UAConfig
from uaclient.entitlements import fips
from uaclient.entitlements.anbox import AnboxEntitlement
from uaclient.entitlements.base import UAEntitlement
from uaclient.entitlements.cc import CommonCriteriaEntitlement
from uaclient.entitlements.cis import CISEntitlement
from uaclient.entitlements.entitlement_status import ApplicabilityStatus
from uaclient.entitlements.esm import ESMAppsEntitlement, ESMInfraEntitlement
from uaclient.entitlements.landscape import LandscapeEntitlement
from uaclient.entitlements.livepatch import LivepatchEntitlement
from uaclient.entitlements.realtime import RealtimeKernelEntitlement
from uaclient.entitlements.repo import RepoEntitlement
from uaclient.entitlements.ros import ROSEntitlement, ROSUpdatesEntitlement
from uaclient.entitlements.usg import USGEntitlement
from uaclient.exceptions import EntitlementNotFoundError


def get_entitlement_classes() -> List[Type[UAEntitlement]]:
    result = [
        AnboxEntitlement,
        CommonCriteriaEntitlement,
        ESMAppsEntitlement,
        ESMInfraEntitlement,
        fips.FIPSEntitlement,
        fips.FIPSUpdatesEntitlement,
        fips.FIPSPreviewEntitlement,
        LandscapeEntitlement,
        LivepatchEntitlement,
        RealtimeKernelEntitlement,
        ROSEntitlement,
        ROSUpdatesEntitlement,
    ]

    series = system.get_release_info().series
    if series in ("xenial", "bionic"):
        result.append(CISEntitlement)
    else:
        result.append(USGEntitlement)

    return sorted(result, key=lambda x: x.name)


def entitlement_factory(
    cfg: UAConfig,
    name: str,
    variant: str = "",
    purge: bool = False,
    access_only: bool = False,
    extra_args: Optional[List[str]] = None,
) -> UAEntitlement:
    """Returns a UAEntitlement object based on the provided name.

    :param cfg: UAConfig instance
    :param name: The name of the entitlement to return
    :param variant: The variant name to be used
    :param purge: If purge operation is enabled
    :param access_only: If entitlement should be set with access only
    :param extra_args: Extra parameters to create the entitlement

    :raise EntitlementNotFoundError: If no entitlement with the given name is
      found, or if the entitlement exists but no variant with the specified
      name is found.
    """

    for entitlement in get_entitlement_classes():
        if name == entitlement.name:
            ent = entitlement(
                cfg=cfg,
                access_only=access_only,
                purge=purge,
                extra_args=extra_args,
            )
            if not variant:
                return ent
            if variant in ent.variants:
                return ent.variants[variant](
                    cfg=cfg,
                    purge=purge,
                    extra_args=extra_args,
                )
            raise EntitlementNotFoundError(entitlement_name=variant)
    raise EntitlementNotFoundError(entitlement_name=name)


def valid_services(cfg: UAConfig) -> List[str]:
    """Return a list of valid services."""
    return sorted(
        [
            entitlement_cls(cfg=cfg).presentation_name
            for entitlement_cls in get_entitlement_classes()
        ]
    )


def order_entitlements_for_enabling(
    cfg: UAConfig, ents: List[str]
) -> List[str]:
    """
    A function to sort entitlments for enabling that preserves invalid names
    """
    valid_ents_ordered = entitlements_enable_order(cfg)

    def sort_order_with_nonexistent_last(ent):
        try:
            return valid_ents_ordered.index(ent)
        except ValueError:
            return len(valid_ents_ordered)

    return sorted(ents, key=lambda ent: sort_order_with_nonexistent_last(ent))


@enum.unique
class SortOrder(enum.Enum):
    REQUIRED_SERVICES = object()
    DEPENDENT_SERVICES = object()


def entitlements_disable_order(cfg: UAConfig) -> List[str]:
    """
    Return the entitlements disable order based on dependent services logic.
    """
    return _sort_entitlements(cfg=cfg, sort_order=SortOrder.DEPENDENT_SERVICES)


def entitlements_enable_order(cfg: UAConfig) -> List[str]:
    """
    Return the entitlements enable order based on required services logic.
    """
    return _sort_entitlements(cfg=cfg, sort_order=SortOrder.REQUIRED_SERVICES)


def _sort_entitlements_visit(
    cfg: UAConfig,
    ent_cls: Type[UAEntitlement],
    sort_order: SortOrder,
    visited: Dict[str, bool],
    order: List[str],
):
    if ent_cls.name in visited:
        return

    ent = ent_cls(cfg)

    if sort_order == SortOrder.REQUIRED_SERVICES:
        cls_list = [e.entitlement for e in ent.required_services]
    else:
        cls_list = list(ent.dependent_services)

    for cls_dependency in cls_list:
        if ent_cls.name not in visited:
            _sort_entitlements_visit(
                cfg=cfg,
                ent_cls=cls_dependency,
                sort_order=sort_order,
                visited=visited,
                order=order,
            )

    order.append(str(ent_cls.name))
    visited[str(ent_cls.name)] = True


def _sort_entitlements(cfg: UAConfig, sort_order: SortOrder) -> List[str]:
    order = []  # type: List[str]
    visited = {}  # type: Dict[str, bool]

    for ent_cls in get_entitlement_classes():
        _sort_entitlements_visit(
            cfg=cfg,
            ent_cls=ent_cls,
            sort_order=sort_order,
            visited=visited,
            order=order,
        )

    return order


def get_valid_entitlement_names(names: List[str], cfg: UAConfig):
    """Return a list of valid entitlement names.

    :param names: List of entitlements to validate
    :return: a tuple of List containing the valid and invalid entitlements
    """
    known_services = valid_services(cfg=cfg)
    entitlements_found = []

    for ent_name in names:
        if ent_name in known_services:
            entitlements_found.append(ent_name)

    entitlements_not_found = sorted(set(names) - set(entitlements_found))

    return entitlements_found, entitlements_not_found


def create_enable_entitlements_not_found_error(
    entitlements_not_found, cfg: UAConfig
) -> exceptions.UbuntuProError:
    """
    Constructs the MESSAGE_INVALID_SERVICE_OP_FAILURE message
    based on the attempted services and valid services.
    """
    valid_services_names = valid_services(cfg=cfg)
    valid_names = ", ".join(valid_services_names)
    service_msg = "\n".join(
        textwrap.wrap(
            "Try " + valid_names + ".",
            width=80,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )
    return exceptions.InvalidServiceOpError(
        operation="enable",
        invalid_service=", ".join(entitlements_not_found),
        service_msg=service_msg,
    )


def check_entitlement_apt_directives_are_unique(
    cfg: UAConfig,
) -> bool:
    entitlement_directives = defaultdict(list)

    for ent_name in valid_services(cfg):
        ent = entitlement_factory(cfg, ent_name)

        if not isinstance(ent, RepoEntitlement):
            continue

        applicability_status, _ = ent.applicability_status()

        if applicability_status == ApplicabilityStatus.APPLICABLE:
            apt_url = ent.apt_url
            apt_suites = ent.apt_suites or ()

            for suite in apt_suites:
                entitlement_directive = ent.repo_policy_check_tmpl.format(
                    apt_url, suite
                )
                entitlement_directives[entitlement_directive].append(
                    {
                        "from": ent_name,
                        "apt_url": apt_url,
                        "suite": suite,
                    }
                )

        for def_path, ent_directive in entitlement_directives.items():
            if len(ent_directive) > 1:
                ent_apt_url = ent_directive[0]["apt_url"]
                ent_suite = ent_directive[0]["suite"]

                raise exceptions.EntitlementsAPTDirectivesAreNotUnique(
                    url=cfg.contract_url,
                    names=", ".join(
                        sorted(str(ent["from"]) for ent in ent_directive)
                    ),
                    apt_url=ent_apt_url,
                    suite=ent_suite,
                )

    return True


def get_title(cfg: UAConfig, ent_name: str, variant=""):
    try:
        return entitlement_factory(cfg, ent_name, variant=variant).title
    except exceptions.UbuntuProError:
        return ent_name
