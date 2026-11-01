# Maintainer: Michal <zmudzin>
pkgname=egg-mouse-battery-indicator-git
pkgver=0.1.0
pkgrel=1
pkgdesc="System tray battery monitor and performance settings for Endgame Gear wireless mice (Linux)"
arch=('any')
url="https://github.com/zmudzin/egg-mouse-battery-indicator"
license=('GPL-3.0-or-later')
depends=('python' 'pyside6')
makedepends=('git' 'python-build' 'python-installer' 'python-setuptools' 'python-wheel')
provides=('egg-mouse-battery-indicator')
conflicts=('egg-mouse-battery-indicator')
source=("git+https://github.com/zmudzin/egg-mouse-battery-indicator.git")
sha256sums=('SKIP')

pkgver() {
    cd "${srcdir}/${pkgname%-git}"
    git describe --long --tags --always 2>/dev/null | sed 's/\([^-]*-g\)/r\1/;s/-/./g' || echo "0.1.0"
}

build() {
    cd "${srcdir}/${pkgname%-git}"
    python -m build --wheel --no-isolation
}

package() {
    cd "${srcdir}/${pkgname%-git}"
    python -m installer --destdir="${pkgdir}" dist/*.whl

    # udev rule
    install -Dm644 udev/70-endgame-gear.rules "${pkgdir}/usr/lib/udev/rules.d/70-endgame-gear.rules"

    # systemd user service
    install -Dm644 systemd/egg-mouse-battery-indicator.service "${pkgdir}/usr/lib/systemd/user/egg-mouse-battery-indicator.service"

    # desktop entry
    install -Dm644 autostart/egg-mouse-battery-indicator.desktop "${pkgdir}/usr/share/applications/egg-mouse-battery-indicator.desktop"

    # license
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
}
