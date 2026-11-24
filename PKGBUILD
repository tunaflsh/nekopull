# Maintainer: tunaflsh <tunaflish.ai@gmail.com>
pkgname=nekopull
pkgver=0.1.0
pkgrel=1
pkgdesc='Poll nekoBT by media id and hand new episodes to Transmission'
arch=('any')
url='https://github.com/tunaflsh/nekopull'
license=('MIT')
depends=('python')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')  # run updpkgsums once the tag exists

build() {
  cd "$pkgname-$pkgver"
  python3 -m build --wheel --no-isolation
}

check() {
  cd "$pkgname-$pkgver"
  python3 -m unittest discover -s tests -v
}

package() {
  cd "$pkgname-$pkgver"
  python3 -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 config.example.toml "$pkgdir/usr/share/doc/$pkgname/config.example.toml"
  install -Dm644 contrib/nekopull.service "$pkgdir/usr/lib/systemd/user/nekopull.service"
  install -Dm644 contrib/nekopull.timer "$pkgdir/usr/lib/systemd/user/nekopull.timer"
}
